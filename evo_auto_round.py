"""evo_auto_round.py — Auto evolution round step for the server-side scheduler.

Invoked by tl_ego_server._bg_run_adapter_pipeline after the three legacy
adapter/promotion scripts. Chain of provenance per round:

    evolution trigger (internal signal, P0) inside resting tick
      -> l10_meta_algorithm_generator.jsonl   (fresh generator output)
        -> l10_gate_policy_bridge.py          (semantic map to gate primitives)
          -> run_evolution_round_r3.py logic  (instantiate, score, register, deploy)
            -> .lae/evolution_round_state.json round bump
              -> l10/l10_l5_patch_bridge.py   (algorithm_selection candidate ->
                                              L5 repair.json proposal; proposal
                                              only, apply stays behind the L5
                                              three-gate; REMOUNTED 2026-10-10)

Design constraints:
  - NEVER blocks the tick loop: runs as a daemon thread, subprocess-isolated.
  - Uses the SAME venv interpreter as the server (sys.executable).
  - Idempotent: skips if bridge JSON is newer than the last processed record
    (tick guard stored in .lae/evo_auto_round_state.json).
  - Never touches frozen suite assets.

Usage (manual):
    H:/unsloth_env/Scripts/python.exe evo_auto_round.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# GBK-console safety (restored 2026-10-10 alongside the Step-3 remount): the
# 09-11 rc=1 crash class was children emitting GBK bytes while the parent
# decoded utf-8 (U+FFFD -> UnicodeEncodeError). Force utf-8 end to end.
os.environ["PYTHONIOENCODING"] = "utf-8:replace"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_DIR = Path(__file__).resolve().parent
WORKSPACE = PROJECT_DIR.parent.parent
LAE = WORKSPACE / ".lae"
QD_DIR = LAE / "qd_archive"

BRIDGE = PROJECT_DIR / "l10_gate_policy_bridge.py"
ROUND_SCRIPT = PROJECT_DIR / "run_evolution_round_r3.py"
L5_BRIDGE = PROJECT_DIR / "l10" / "l10_l5_patch_bridge.py"
DRAFTER = PROJECT_DIR / "l10" / "l10_draft_algorithm_candidate.py"
PS1_APPLY = WORKSPACE / "scripts" / "l5_apply_repair.ps1"
ARCH_APPROVE = PROJECT_DIR / "arch_auto_approve.py"
PROPOSALS = LAE / "l10_l5_proposals.jsonl"
STATE_PATH = LAE / "evo_auto_round_state.json"
GEN_LOG = LAE / "l10_meta_algorithm_generator.jsonl"
ROUND_STATE = LAE / "evolution_round_state.json"


def _load(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _last_gen_tick() -> int:
    """Read the last triggered record's tick from the concatenated-JSON log."""
    try:
        raw = GEN_LOG.read_text(encoding="utf-8")
        dec = json.JSONDecoder()
        idx, last = 0, None
        while idx < len(raw):
            while idx < len(raw) and raw[idx] in " \t\r\n":
                idx += 1
            if idx >= len(raw):
                break
            try:
                obj, end = dec.raw_decode(raw, idx)
                idx = end
                if obj.get("triggered"):
                    last = obj.get("tick")
            except json.JSONDecodeError:
                nl = raw.find("\n", idx)
                if nl == -1:
                    break
                idx = nl + 1
        return int(last or 0)
    except Exception:
        return 0


def _tail(s: str, n: int = 4) -> str:
    return "\n".join((s or "").splitlines()[-n:])


def _fresh_param_proposals(t0: float, tail_bytes: int = 65536,
                           statuses: tuple = ("param_proposed",)) -> list[dict]:
    """本次运行窗口内 bridge 新写的 param_proposed 行(不只最后一条)。

    2026-11-10: 新族(TypeBudgetKWTA)落的是成对参数(budget + enable), 旧版
    _last_proposal_record 只取最后一条 -> 永远只写到其中一个, 另一个被静默
    丢弃; 且最新 generator 记录轮换快, 上一轮提案没被消费就永久搁浅。
    statuses 可放宽到 param_unmappable —— 信号聚合要用"全灭"的病轮。
    """
    out = []
    try:
        with open(PROPOSALS, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            for ln in f.read().decode("utf-8", "replace").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if (r.get("status") in statuses
                        and float(r.get("ts", 0) or 0) >= t0 - 5):
                    out.append(r)
    except Exception:
        return []
    return out


def _last_proposal_record():
    """提案日志的最后一条(任何 status)。"""
    last = None
    try:
        for ln in PROPOSALS.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln:
                try:
                    last = json.loads(ln)
                except Exception:
                    continue
    except Exception:
        return None
    return last


def _apply_param(key, value, reason: str) -> str:
    """POST /apply-parameter — 参数唯一的写入口(白名单+范围+审计+重启重放)。"""
    if not key or value is None:
        return "no_key"
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:8766/apply-parameter",
            data=json.dumps({"key": key, "value": value, "reason": str(reason)[:200],
                             "source": "l10_candidate"}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode("utf-8"))
        return "applied" if res.get("status") == "ok" else f"rejected:{str(res.get('error'))[:60]}"
    except Exception as e:
        return f"exc:{str(e)[:60]}"


def _fresh_proposal(t0: float):
    """本次运行内 bridge 新写的 proposed 提案 (含 workspace 相对沙箱路径)。"""
    last = _last_proposal_record()
    if not last or float(last.get("ts", 0) or 0) < t0 - 5:
        return None
    if last.get("status") != "proposed" or not last.get("sandbox"):
        return None
    rel = Path(str(last["sandbox"]))
    if rel.is_absolute():
        try:
            rel = rel.relative_to(WORKSPACE)
        except ValueError:
            return None
    return {"candidate_id": last.get("candidate_id"), "sandbox_rel": str(rel).replace("\\", "/")}


def _heavy_job_running() -> bool:
    """最佳努力: 矩阵/进化轮在跑时推迟 apply (带重启的修复会撞上它们)。"""
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        txt = (p.stdout or "").lower()
        return any(m in txt for m in ("run_minecraft_benchmark", "run_evolution_round"))
    except Exception:
        return False


def run(force: bool = False) -> int:
    st = _load(STATE_PATH, {"last_gen_tick": -1, "runs": 0, "last_run_ts": "", "last_result": ""})
    gen_tick = _last_gen_tick()

    if not force:
        if gen_tick <= int(st.get("last_gen_tick", -1)):
            # no fresh generator output since last auto-round: skip silently
            print(f"[evo-auto] no fresh L10 output (gen_tick={gen_tick} "
                  f"<= last={st.get('last_gen_tick')}); skipping", flush=True)
            return 0
        # also require at least one new round completed since last run to avoid
        # hammering on every trigger while cooldown re-arms — min spacing handled
        # by caller's 50-tick throttle; here we just guard duplicates.

    print(f"[evo-auto] running auto-round (gen_tick={gen_tick}, prev={st.get('last_gen_tick')})", flush=True)

    # Step 1: refresh bridge blueprints from latest L10 output
    rc1 = subprocess.run(
        [sys.executable, str(BRIDGE)],
        cwd=str(WORKSPACE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    tail1 = "\n".join((rc1.stdout or "").splitlines()[-4:])
    print(f"[evo-auto] bridge rc={rc1.returncode}\n{tail1}", flush=True)
    if rc1.returncode != 0:
        st.update({"last_run_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "last_gen_tick": st.get("last_gen_tick", -1),
                   "last_result": f"bridge_failed:{rc1.returncode}"})
        _save(STATE_PATH, st)
        return rc1.returncode or 1

    # Step 2: run the R3-style round (score + registry + deploy)
    rc2 = subprocess.run(
        [sys.executable, str(ROUND_SCRIPT)],
        cwd=str(WORKSPACE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    tail2 = "\n".join((rc2.stdout or "").splitlines()[-12:])
    print(f"[evo-auto] round rc={rc2.returncode}\n{tail2}", flush=True)

    # Step 3: L10 -> L5 algorithm channel (rearmed 2026-10-10: supply floor +
    # gated auto-apply). Order matters: the drafter runs BEFORE the bridge so a
    # freshly drafted candidate is the newest generator record the bridge reads
    # (the bridge only inspects the LAST triggered record).
    # 3a) supply floor: no algorithm_selection candidate in 72h -> open one
    #     self-reflection window via the real generator (l10_draft_algorithm_candidate.py).
    # 3b) bridge: latest candidate -> ego_sandbox/l10g-*/repair.json proposal.
    # 3c) auto-apply: a FRESH proposal goes through the L5 three-gate unattended
    #     (L5_APPROVAL=n stops the operator prompt from hanging; gate1 still
    #     decides — non-eligible patches abort cleanly).
    # 3d/3e) architecture channel: autonomous approval (user-granted gate policy)
    #     + queue an implementation goal for an approved blueprint.
    # Verify by the log lines, not this comment: "[evo-auto] l10-drafter rc=...".
    l5_note = "l10_l5=missing"
    t0 = time.time()
    seen = list((st.get("l10_apply_seen") or [])[:10])
    if DRAFTER.exists():
        try:
            rc0 = subprocess.run(
                [sys.executable, str(DRAFTER)],
                cwd=str(WORKSPACE), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=240,
            )
            print(f"[evo-auto] l10-drafter rc={rc0.returncode}\n{_tail(rc0.stdout, 3)}", flush=True)
            l5_note = "draft=ok" if rc0.returncode == 0 else f"draft=rc{rc0.returncode}"
        except Exception as _de:
            l5_note = "draft=exc"
            print(f"[evo-auto] l10-drafter exception: {_de}", flush=True)
    else:
        print(f"[evo-auto] l10 drafter missing: {DRAFTER}", flush=True)

    if L5_BRIDGE.exists():
        try:
            rc3 = subprocess.run(
                [sys.executable, str(L5_BRIDGE)],
                cwd=str(WORKSPACE), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
            tail3 = _tail(rc3.stdout, 4)
            print(f"[evo-auto] l10-l5-bridge rc={rc3.returncode}\n{tail3}", flush=True)
            l5_note += " l10_l5=" + ("ok" if rc3.returncode == 0 else f"rc{rc3.returncode}")
        except Exception as _l5e:
            l5_note += " l10_l5=exc"
            print(f"[evo-auto] l10-l5-bridge exception: {_l5e}", flush=True)
    else:
        print(f"[evo-auto] l10-l5 bridge missing: {L5_BRIDGE}", flush=True)

    # 3c) gated auto-apply of a fresh proposal (never re-apply an attempted one)
    if PS1_APPLY.exists():
        prop = _fresh_proposal(t0)
        if prop and prop["candidate_id"] in seen:
            l5_note += " apply=seen"
        elif prop:
            if _heavy_job_running():
                l5_note += " apply=deferred(busy)"
            else:
                try:
                    rc4 = subprocess.run(
                        ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-File", str(PS1_APPLY), "-SandboxDir", prop["sandbox_rel"]],
                        cwd=str(WORKSPACE), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=1800,
                        env={**os.environ, "L5_APPROVAL": "n"},
                    )
                    out = (rc4.stdout or "") + "\n" + (rc4.stderr or "")
                    if "Aborted by operator" in out:
                        verdict = "blocked_gate1"
                    elif "GATE 3 FAILED" in out or "rolled back" in out.lower():
                        verdict = "rolled_back"
                    elif rc4.returncode == 0:
                        verdict = "applied"
                    else:
                        verdict = f"rc{rc4.returncode}"
                    l5_note += f" apply={verdict}"
                    print(f"[evo-auto] l10-apply {prop['candidate_id']} -> {verdict}\n{_tail(out, 6)}", flush=True)
                except Exception as _ae:
                    l5_note += " apply=exc"
                    print(f"[evo-auto] l10-apply exception: {_ae}", flush=True)
                seen = (seen + [prop["candidate_id"]])[-10:]
    else:
        print(f"[evo-auto] L5 applier missing: {PS1_APPLY}", flush=True)

    # 3c-param) 参数面处方 -> 既有写回通道(白名单+范围+审计+重启重放)。
    # 2026-10-15: 新形状 L10 候选(strategy/code_anchor)的唯一可落地形态。用的是
    # retained→runtime hook 同一条 _apply_runtime_param, 不新增机制。
    _lp = _last_proposal_record()
    _fresh = _fresh_param_proposals(t0)
    if _lp and _lp.get("status") == "param_proposed" and float(_lp.get("ts", 0) or 0) >= t0 - 5 and not _fresh:
        _fresh = [_lp]  # 兼容: 尾部窗口没圈到但最后一条够新
    for _lp in _fresh:
        _pcid = str(_lp.get("candidate_id") or "")
        _pkey = str((_lp.get("param") or {}).get("key") or "")
        _dedup = f"{_pcid}:{_pkey}"  # 成对参数同 cid, 按 (cid,key) 去重
        if _dedup in seen:
            l5_note += f" param={_pkey}:seen"
            continue
        _pr = _lp.get("param") or {}
        _pv = _apply_param(_pr.get("key"), _pr.get("value"), _lp.get("reason") or "")
        l5_note += f" param={_pv}"
        print(f"[evo-auto] l10-param {_pcid} {_pr.get('key')}={_pr.get('value')} "
              f"-> {_pv} ({_lp.get('reason')})", flush=True)
        seen = (seen + [_dedup])[-10:]

    if ARCH_APPROVE.exists():
        for _args, _label in (([], "approve"), (["--queue-auto"], "queue")):
            try:
                rc5 = subprocess.run(
                    [sys.executable, str(ARCH_APPROVE), *_args],
                    cwd=str(WORKSPACE), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60,
                )
                print(f"[evo-auto] arch-{_label} rc={rc5.returncode}\n{_tail(rc5.stdout, 2)}", flush=True)
            except Exception as _ape:
                print(f"[evo-auto] arch-{_label} exception: {_ape}", flush=True)
    else:
        print(f"[evo-auto] arch approver missing: {ARCH_APPROVE}", flush=True)

    rs = _load(ROUND_STATE, {})
    # 三信号进轮次记录 (2026-11-10): 用户判据"让死类型数/签名冲突率/新族产出率
    # 进入候选池, 让它们在闭环里被评价"。此前信号只挂在提案行上、无消费者 ——
    # 聚合本窗口内**全部** bridge 行(含 param_unmappable): "全灭"的病轮才是
    # 信号最该被看见的轮 (family_yield=2/conflict_rate/dead 数全在里面)。
    _sig = {}
    for _lp in _fresh_param_proposals(t0, statuses=("param_proposed", "param_unmappable")):
        for k, v in (_lp.get("signals") or {}).items():
            _sig[k] = v  # 同轮多行: 后写覆盖, 值同源无冲突
    if _sig:
        st["last_signals"] = _sig
        st["signals_history"] = (list(st.get("signals_history") or [])[-49:]
                                 + [{"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "run": st.get("runs"), **_sig}])
    st.update({
        "last_gen_tick": gen_tick if rc2.returncode == 0 else st.get("last_gen_tick", -1),
        "runs": int(st.get("runs", 0)) + (1 if rc2.returncode == 0 else 0),
        "last_run_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "l10_apply_seen": seen,
        "last_result": ("round_ok" if rc2.returncode == 0 else f"round_failed:{rc2.returncode}")
                       + (f" next_baseline={rs.get('next_baseline')}" if rs.get("next_baseline") else "")
                       + f" {l5_note}",
    })
    _save(STATE_PATH, st)
    return rc2.returncode


if __name__ == "__main__":
    ap_force = "--force" in sys.argv
    raise SystemExit(run(force=ap_force))
