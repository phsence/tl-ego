#!/usr/bin/env python3
"""probe_specs.py — 单集探针：给定策略参数跑一小撮 MC 任务，写一份 probe 结果。

契约（唯一调用方 = ego_evolution_loop.py；签名从它的 subprocess 调用倒推）::

    python -B probe_specs.py --params '<json>' --specs "mc-holdout-104,mc-holdout-209" [--baseline]
      -> .lae/probes/probe-ego-<unixts>.json
         {profile, sha8, changed, bridge_sha256, generation_id, ts,
          rows: [{spec, ok, min_distance, dy, elapsed_s, fail_reason}]}

2026-10-10 重建（原文件在 09-30 wipe 中丢失，全机无副本）。口径对齐
`ego-active-evolution` 技能 B 路径表：Gate1 校验 → 写 profile → **等桥拾取 sha8 才开跑** →
跑完**恢复 baseline profile + goal 文件**。params 语义 = 传入键**叠加**在 baseline 默认之上
（"只叠加你传的键"）。信息字段（profile/sha8/changed/bridge_sha256）仅供审计，调用方只读
generation_id + rows。

安全：
  * --check：只解析并打印计划，不碰世界（不连 RCON、不切 profile）。
  * 任何单集异常 → 中止（不落文件），让调用方把本轮记为"探针不完整"。
  * 结束时（含异常路径）无条件恢复 baseline profile + goal 文件 —— 避免实验参数/陈旧
    goal 留在桥上劫持身体（事故模式见 tl-ego-evolution-pipeline 技能）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WS = Path(__file__).resolve().parent
if str(WS) not in sys.path:
    sys.path.insert(0, str(WS))

import policy_runtime as pr                                 # noqa: E402
from capability_suites.minecraft import tasks as mc_tasks    # noqa: E402
from capability_suites.minecraft.verifier import MinecraftRCON  # noqa: E402

PROBES = WS / ".lae" / "probes"
BRIDGE_LOG = Path("C:/tools/mc/bot/tl-ego-bridge-run.log")
GOAL = Path(mc_tasks.GOAL_FILE)   # executor 的 default_goal_writer 写的就是这个通道
_POLICY_LINE = re.compile(r"\[bridge\] policy profile loaded: (\S+) sha=([0-9a-f]{8})")


def _bridge_policy_pickup():
    try:
        text = BRIDGE_LOG.read_bytes()[-200_000:].decode("utf-8", "replace")
    except OSError:
        return None
    hits = _POLICY_LINE.findall(text)
    return hits[-1] if hits else None


def _wait_for_pickup(sha8: str, timeout_s: float):
    deadline = time.time() + timeout_s
    hit = None
    while time.time() < deadline:
        hit = _bridge_policy_pickup()
        if hit and hit[1] == sha8:
            return True, hit
        time.sleep(3)
    return False, hit


def _generation_id() -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8766/status", timeout=4) as fh:
            doc = json.loads(fh.read().decode("utf-8", "replace"))
        return str(doc.get("runtime_generation_id") or doc.get("generation_id") or "unknown")
    except Exception:
        return "unknown"


def _resolve_specs(arg: str) -> list:
    by_id = {s.episode_id: s for s in (mc_tasks.dev_specs() + mc_tasks.holdout_specs())}
    ids = [x.strip() for x in arg.split(",") if x.strip()]
    missing = [x for x in ids if x not in by_id]
    if missing:
        raise SystemExit(f"unknown spec ids: {missing} (sample available: {sorted(by_id)[:5]})")
    return [by_id[x] for x in ids]


def _restore_baseline() -> None:
    """全默认 baseline 恢复上线。

    对齐技能 recipe：write_profile 默认 write=False = 纯 dry-run（只算 sha 不落盘），
    activate 加载**已存在**的 baseline.json —— 不重写文件（历史 active.json 的
    baseline sha 与 09-14 创建的文件一致，证明原版恢复也不重写）。activated_by=probe_restore。
    """
    try:
        doc = pr.write_profile({}, profile_id=pr.BASELINE_ID)
        ptr = pr.activate(pr.BASELINE_ID, activated_by="probe_restore")
        sha8 = str(ptr.get("sha256") or doc.get("sha256") or "")[:8]
        ok, hit = _wait_for_pickup(sha8, 45.0) if sha8 else (False, None)
        print(f"[probe] baseline 恢复: sha={sha8} bridge_pickup={ok} {hit or ''}")
    except Exception as e:  # 恢复失败必须可见，但不能再抛（finally 里）
        print(f"[probe] baseline 恢复失败: {type(e).__name__}: {e}")


def _restore_goal(goal_backup) -> None:
    """把 ego-goal.txt 恢复成探测前内容（原不存在则清空）—— 陈旧 goal 会劫持身体。"""
    try:
        if goal_backup is not None:
            GOAL.write_bytes(goal_backup)
            print(f"[probe] goal 恢复: 原样写回 {len(goal_backup)} 字节")
        elif GOAL.exists():
            GOAL.write_bytes(b"")
            print("[probe] goal 恢复: 清空（探测前不存在）")
    except Exception as e:
        print(f"[probe] goal 恢复失败: {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params", required=True, help="JSON dict of policy params")
    ap.add_argument("--specs", required=True, help="comma-separated episode ids")
    ap.add_argument("--baseline", action="store_true", help="label this run as the parent/baseline arm")
    ap.add_argument("--check", action="store_true", help="resolve + print plan only (no world access)")
    ap.add_argument("--pickup-timeout", type=float, default=90.0)
    args = ap.parse_args()

    params = json.loads(args.params)
    validated, notes = pr.validate_params(dict(params))
    bad = [n for n in notes if n.startswith("rejected_")]
    if bad:
        raise SystemExit(f"params rejected by Gate1: {bad}")
    specs = _resolve_specs(args.specs)
    defaults = pr.PARAM_SPEC_DEFAULTS()
    effective = {**defaults, **validated}   # 传入键叠加在 baseline 默认之上
    changed = {k: v for k, v in validated.items() if defaults.get(k) != v}
    ts = int(time.time())

    if args.check:
        print(json.dumps({
            "check": True,
            "specs": [s.episode_id for s in specs],
            "params_effective": effective,
            "changed": changed,
            "baseline_label": args.baseline,
            "would_write": str(PROBES / f"probe-ego-{ts}.json"),
            "bridge_log": str(BRIDGE_LOG),
            "goal_file": str(GOAL),
        }, ensure_ascii=False, indent=1))
        return 0

    profile_id = f"probe-ego-{ts}"
    written = pr.write_profile(effective, profile_id=profile_id, parent_id=pr.BASELINE_ID, write=True)
    ptr = pr.activate(profile_id, activated_by="probe_specs")
    sha = str(ptr.get("sha256") or written.get("sha256") or "")
    sha8 = sha[:8]
    print(f"[probe] profile={profile_id} sha={sha8} baseline_label={args.baseline}")
    if sha8:
        picked, hit = _wait_for_pickup(sha8, args.pickup_timeout)
        if not picked:
            print(f"[probe] 桥未确认加载 {sha8}（最后一条 {hit}）→ 中止，不落文件")
            _restore_baseline()
            return 1

    goal_backup = GOAL.read_bytes() if GOAL.exists() else None
    print(f"[probe] goal 备份: " + (f"{len(goal_backup)} 字节" if goal_backup is not None else "不存在"))

    client = MinecraftRCON()
    rows = []
    try:
        executor = mc_tasks.make_reach_executor(client)
        for spec in specs:
            t0 = time.time()
            outcome = executor(spec, effective)
            tr = outcome.result
            ev = outcome.evidence or {}
            elapsed = float(tr.elapsed_s) if tr.elapsed_s else (time.time() - t0)
            rows.append({
                "spec": spec.episode_id,
                "ok": bool(tr.success),
                "min_distance": ev.get("min_distance"),
                "dy": ev.get("dy"),
                "elapsed_s": round(elapsed, 1),
                "fail_reason": ev.get("fail_reason"),
            })
            print(f"[probe] {spec.episode_id}: ok={rows[-1]['ok']} "
                  f"min_distance={rows[-1]['min_distance']} dy={rows[-1]['dy']} "
                  f"elapsed_s={rows[-1]['elapsed_s']}")
    except Exception as e:
        print(f"[probe] episode 异常 → 中止，不落文件: {type(e).__name__}: {e}")
        return 1
    finally:
        client.close()
        _restore_baseline()
        _restore_goal(goal_backup)

    PROBES.mkdir(parents=True, exist_ok=True)
    out = PROBES / f"probe-ego-{ts}.json"
    out.write_text(json.dumps({
        "profile": profile_id,
        "sha8": sha8,
        "changed": changed,
        "bridge_sha256": sha,   # informational: full sha of the activated profile
        "generation_id": _generation_id(),
        "ts": time.time(),
        "rows": rows,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"[probe] written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
