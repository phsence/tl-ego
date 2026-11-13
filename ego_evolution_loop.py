#!/usr/bin/env python3
"""自转闭环驱动：把"读判定 → 问 ego → 门控 → 探针 → (可选)矩阵 → 判定写回"接成一条链。

    python -B ego_evolution_loop.py --check          # 只打印它打算做什么（安全，不碰世界）
    python -B ego_evolution_loop.py                  # 跑一圈（默认不烧矩阵，只到探针）
    python -B ego_evolution_loop.py --matrix         # 探针通过就接着点火完整矩阵
    python -B ego_evolution_loop.py --loop --interval-min 180   # 常驻自转

设计（刻意的懒）：
  * 不重写逻辑——ego_self_proposal.py / probe_specs.py / run_minecraft_benchmark.py 都是 CLI，
    本驱动只用 subprocess 调它们 + 读它们落盘的结果文件。
  * 幂等：`.lae/self_proposals/loop_state.json` 记已处理的报告；同一份报告只处理一次。
  * 有界：`--max-matrices-per-day`（默认 2）。世界长跑会退化，无上限自转=自欺。
  * 防撞车：锁文件 + 检测正在跑的矩阵/探针进程；锁新鲜就退出（cron 与手工互不踩）。
  * 默认**不**自动烧矩阵：矩阵会扰动整个世界 2 小时（用户可能正在地图里玩），
    要无人值守就跑 `--matrix`。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WS = Path(__file__).resolve().parent
LAE = WS / ".lae"
STATE = LAE / "self_proposals" / "loop_state.json"
LOCK = LAE / "self_proposals" / "loop.lock"
LOG = LAE / "self_proposals" / "loop_log.jsonl"
BENCH = LAE / "benchmarks"
PROBES = LAE / "probes"
PROPOSALS = LAE / "self_proposals"
PY = sys.executable or "python"

WINDOW = 300          # 卷子参数：双臂必须同值
KEY = "reobserve_after_no_motion_s"


# ---------------------------------------------------------------- 小工具

def _newest(d: Path, pat: str, skip: set[str] | None = None) -> Path | None:
    """最新一份匹配文件；skip 里的名字（已处理）跳过。

    2026-09-29: 加 skip 是因为"最新"不等于"未处理"。原版只看 mtime 最新,
    一旦那一份进了 processed_reports, 每轮都撞幂等早退(one_cycle:204)——
    后面 28 份真实证据永远排在它后面出不来(实测 benchmarks 33 份 / 已处理 5 份)。
    同一份函数修一次, _newest 的三个调用点(proposals 那个不该过滤)只有报告这条要传。
    """
    fs = sorted(d.glob(pat), key=lambda p: p.stat().st_mtime)
    if skip:
        fs = [p for p in fs if p.name not in skip]
    return fs[-1] if fs else None


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"processed_reports": [], "matrices": []}


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _log(row: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), **row}, ensure_ascii=False) + "\n")


KNOWN_BOTS = {"ego", "aria", "bruno", "jkon",
              "numenviewer", "egoowner"}   # numen 同伴/机器人/观察客户端/主人账号 —— 都不算"有人在玩"
# 2026-09-19 修: 原白名单漏了 NumenViewer(3007 观察客户端) 与 EgoOwner(numen 客户端登录账号),
# 世界门因此恒 False → 自转永远跳过一切碰世界的动作。


def _busy() -> str | None:
    """有矩阵/探针在跑 → 返回原因；否则 None。

    坑：git-bash 的 `ps -W` 只给可执行文件路径、**不带命令行参数** → 匹配不到
    `run_minecraft_benchmark`；必须走 PowerShell 的 Win32_Process.CommandLine。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             # 2026-09-19 修: 只认 python.exe —— 旧写法不限进程名, PowerShell 自己/任何
             # 命令行里含这两个词的进程(bash 包装、编辑中的 shell)都会自匹配 →
             # _busy() 恒真, 自转第一轮就"有任务在跑"退出(实测 tick=19058)。
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object "
             "{ $_.CommandLine -match 'run_minecraft_benchmark|probe_specs' } | "
             "Select-Object -First 1 -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=40).stdout.strip()
        if out:
            return f"running pid={out.splitlines()[0].strip()}"
    except Exception:
        pass
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 9000:
        return f"lock fresh ({int(time.time() - LOCK.stat().st_mtime)}s)"
    return None


def _world_idle() -> tuple[bool, str]:
    """除 ego 和已知机器人外没有玩家在线 = 可以安全占用世界跑探针。

    用户在玩的时候跑探针会把 ego 传送走、放金块 = 可见打扰 → 宁可空转一轮。
    """
    try:
        sys.path.insert(0, str(WS))
        sys.path.insert(0, str(WS / "capability_suites" / "minecraft"))
        from capability_suites.minecraft.verifier import MinecraftRCON
        raw = MinecraftRCON().command("list")
    except Exception as e:  # noqa: BLE001
        return False, f"rcon_unavailable:{type(e).__name__}"
    body = raw.split(":", 1)[-1].strip() if ":" in raw else ""
    names = [n.strip() for n in body.split(",") if n.strip()]
    others = [n for n in names if n.lower() not in KNOWN_BOTS]
    return (not others), f"players={names}"


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(WS), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    tail = "\n".join((p.stdout or "").strip().splitlines()[-6:])
    return p.returncode, tail


def _verdict_word(report: dict) -> str:
    dec = report.get("decision") or {}
    return dec.get("decision") if isinstance(dec, dict) else str(dec)


def _lift_line(report: dict) -> str:
    dec = report.get("decision") or {}
    checks = (dec.get("checks") if isinstance(dec, dict) else {}) or {}
    bits = []
    for k in ("holdout_lift", "efficiency_lift"):
        c = checks.get(k) or {}
        if k == "holdout_lift":
            bits.append(f"holdout_lift={c.get('lift')}(pass={c.get('pass')})")
        else:
            bits.append(f"efficiency ratio={c.get('ratio')}(pass={c.get('pass')})")
    return " ".join(bits)


def _params_from_proposal(path: Path) -> dict | None:
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("status") or not d.get("changed"):
        return None
    return d.get("params") or None


def _targets_from_report(report: dict, max_n: int = 3) -> list[str]:
    """候选输了、基线赢了的 holdout 集 = 该候选没有解决的；用于选探针目标。"""
    recs = {(r.get("arm"), r.get("episode_id")): r for r in report.get("records", [])}
    lost = [ep for (a, ep) in recs
            if a == "candidate_on" and ep.startswith("mc-holdout")
            and not recs[("candidate_on", ep)].get("success")
            and recs.get(("baseline", ep), {}).get("success")]
    return sorted(lost)[:max_n] or ["mc-holdout-100", "mc-holdout-111"]


# ---------------------------------------------------------------- 一圈

def one_cycle(args) -> int:
    st = _load_state()
    done = set(st["processed_reports"])
    # 2026-09-29: 找"最新且未处理"的报告, 不是"最新"的。最新那份若已 processed,
    # 原版每轮都空转退出, 排在其后的真实证据永远轮不到(幂等短路挡在 --matrix 之前)。
    report_path = _newest(BENCH, "*.json", skip=done)
    if report_path is None:
        print(f"[loop] 报告已全部处理过（{len(done)} 份），无新证据"); return 0
    name = report_path.name
    report = json.loads(report_path.read_text(encoding="utf-8"))
    candidate_id = report.get("candidate_id") or "?"
    print(f"[loop] 最新未处理报告 {name}（候选 {candidate_id}）判定 {_verdict_word(report)}｜{_lift_line(report)}")

    if args.check:
        print(f"[loop][check] 将执行: 判定写回 → 问 ego → Gate1 → 探针 {_targets_from_report(report)}"
              f"{' → 矩阵' if args.matrix else '（不烧矩阵，需 --matrix）'}")
        return 0

    busy = _busy()
    if busy:
        print(f"[loop] 有任务在跑，本轮退出: {busy}"); return 0   # 良性空转, 不是失败

    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    try:
        # 1) 判定写回 ego 的历史（闭环的"结果回流"）
        prop_id = candidate_id if candidate_id.startswith("mc-self") else "unknown"
        rc, out = _run([PY, "-B", "ego_self_proposal.py", "--outcome", prop_id,
                        f"矩阵判定={_verdict_word(report)}：{_lift_line(report)}"], 60)
        print(f"[loop] 判定写回 {prop_id}: rc={rc} {out.strip()[:120]}")

        # 2) 问 ego 要候选（它自己的 CLI 已含 Gate1 + 反馈重试）
        rc, out = _run([PY, "-B", "ego_self_proposal.py", "--attempts", "3"], 900)
        print(f"[loop] 问 ego: rc={rc}\n{out}")
        prop = _newest(PROPOSALS, "mc-self-*.json")
        params = _params_from_proposal(prop) if prop else None
        if not params:
            _log({"action": "no_proposal", "report": name, "rc": rc})
            st["processed_reports"].append(name)
            _save_state(st)
            print("[loop] ego 没给出可用候选 → 记录并结束本轮（能力数据，不是故障）")
            return 2

        # 3) 探针：父本 vs 候选（窗口两侧都必须等于卷子参数）
        targets = _targets_from_report(report)
        spec_arg = ",".join(targets)
        idle, why = _world_idle()
        print(f"[loop] 世界空闲检查: {idle}（{why}）")
        if not idle and not args.force_world:
            _log({"action": "probe_skipped_world_busy", "report": name, "why": why,
                  "proposal": prop.name})
            st["processed_reports"].append(name)
            _save_state(st)
            print("[loop] 你在世界里/RCON 不可用 → 本轮不碰世界（--force-world 可强制）")
            return 4
        candidate_params = {**params, "goal_timeout_s": WINDOW}
        base_params = {k: v for k, v in candidate_params.items() if k != KEY}
        for label, prm in (("父本", base_params), ("候选", candidate_params)):
            rc, out = _run([PY, "-B", "probe_specs.py", "--params", json.dumps(prm),
                            "--specs", spec_arg], 3600)
            print(f"[loop] 探针/{label}: rc={rc}\n{out}")
        # 读最近两次探针结果（父本在前、候选在后）
        probes = sorted(PROBES.glob("probe-ego-*.json"), key=lambda p: p.stat().st_mtime)[-2:]
        if len(probes) < 2:
            print("[loop] 探针结果不全 → 中止"); return 1
        got = {}
        for tag, p in zip(("parent", "candidate"), probes):
            d = json.loads(p.read_text(encoding="utf-8"))
            got[tag] = {r["spec"]: bool(r["ok"]) for r in d["rows"]}
        better = sum(1 for s in got["candidate"] if got["candidate"][s] and not got["parent"].get(s))
        worse = sum(1 for s in got["candidate"] if got["parent"].get(s) and not got["candidate"][s])
        verdict = "pass" if worse == 0 and better >= 0 else "reject"
        _log({"action": "probe", "report": name, "proposal": prop.name,
              "parent": got["parent"], "candidate": got["candidate"], "verdict": verdict})
        print(f"[loop] 探针判定: {verdict}（候选更好 {better} 集 / 更差 {worse} 集）")

        if verdict == "reject":
            _run([PY, "-B", "ego_self_proposal.py", "--outcome", prop.stem,
                  f"探针否决: 更好{better}/更差{worse} 集"], 60)
            st["processed_reports"].append(name)
            _save_state(st)
            return 3

        # 4) 可选：点火完整矩阵（有界）
        day = time.strftime("%Y-%m-%d")
        used = [m for m in st["matrices"] if m.get("day") == day]
        if not args.matrix:
            print(f"[loop] 探针通过，但本轮不烧矩阵（--matrix 才点火）。可手工跑: {spec_arg}")
        elif len(used) >= args.max_matrices_per_day:
            print(f"[loop] 今日矩阵已达上限 {args.max_matrices_per_day}，跳过点火")
        else:
            created = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            cmd = [PY, "-B", "run_minecraft_benchmark.py", "--live",
                   "--candidate-id", prop.stem, "--profile-created-at", created,
                   "--dev-limit", "4", "--holdout-limit", "12", "--settle-wait", "180",
                   "--parent-params", json.dumps(base_params),
                   "--candidate-params", json.dumps({**params, "goal_timeout_s": WINDOW})]
            print(f"[loop] 点火矩阵: {' '.join(cmd[:6])} …（2h）")
            st["matrices"].append({"day": day, "proposal": prop.stem, "created": created})
            _save_state(st)
            rc, out = _run(cmd, 10800)
            print(f"[loop] 矩阵 rc={rc}\n{out}")
        st["processed_reports"].append(name)
        _save_state(st)
        return 0
    finally:
        LOCK.unlink(missing_ok=True)


def m_candidate_suffix(candidate_id: str) -> str:
    """把矩阵候选 id 映射回 ego 提案 id（矩阵用提案 id 当 candidate-id）。"""
    return "" if candidate_id.startswith("mc-self") else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只打印计划，不执行")
    ap.add_argument("--matrix", action="store_true", help="探针通过后自动点火完整矩阵")
    ap.add_argument("--loop", action="store_true", help="常驻自转")
    ap.add_argument("--interval-min", type=int, default=180)
    ap.add_argument("--max-matrices-per-day", type=int, default=2)
    ap.add_argument("--force-world", action="store_true",
                    help="世界里有玩家也照跑探针（默认跳过，避免打断用户）")
    args = ap.parse_args()

    if not args.loop:
        return one_cycle(args)
    while True:
        try:
            one_cycle(args)
        except Exception as e:  # noqa: BLE001 - 自转不能因一次异常死掉
            print(f"[loop] cycle error: {type(e).__name__}: {e}")
            _log({"action": "error", "error": f"{type(e).__name__}: {e}"})
        time.sleep(max(60, args.interval_min * 60))


if __name__ == "__main__":
    raise SystemExit(main())
