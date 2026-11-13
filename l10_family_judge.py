"""L10 族评判器：把 shadow 族行变成"真跑过一次的实验"，然后保留或淘汰。

RSI 缺的最后一块不是"能造族"，是"造出来的族没人判"。这里判，判据**预先写死**、
二值、可复算，不看解释：

    注册表族行 → shadow 提案 → 申请一次真参数写（带 TTL 兜底，40 tick 窗口）
      → 窗口里死类型是否真的活过来（observer 行里不再出现在 dead_types，且 ≥2 行）
        → 活过来 = keep（族行留着，继续参与竞争）
        → 没活   = kill（参数立刻回滚 + 删掉该族行 + 写判词，不再重试）

对照组不是形式主义：开跑前同样长度的窗口里它必须也是死的。基线就活着的类型
根本不算"死类型被救活"，那种"赢"是白送。

ponytail: 单进程单实验 + tick 窗口；并发多实验/多臂对照等真有候选堆积再说。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
LAE = WORKSPACE / ".lae"
STATE = LAE / "l10_family_judge_state.json"
PROPOSALS = LAE / "l10_l5_proposals.jsonl"
OBSERVER = LAE / "l10_observer.jsonl"
REGISTRY = LAE / "l10_family_registry"
VERDICTS = LAE / "l10_family_verdicts.jsonl"

LAG = 6                 # 参数写完先等这么多 tick 才可能看到行为变化
WINDOW = 40             # 观察窗（tick）
MIN_ALIVE_ROWS = 2      # 窗口里至少这么多行"它不死了"才算救活
TTL_S = 3600            # 判官自己死掉时由服务器的 TTL reaper 回滚
API = "http://127.0.0.1:8766/apply-parameter"
SRC = "hermes"


# ── 读盘 ────────────────────────────────────────────────────────────────────
def _tail_jsonl(path: Path, max_bytes: int = 4_000_000) -> list[dict]:
    if not path.exists():
        return []
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        raw = f.read().decode("utf-8", "replace")
    lines = raw.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]          # 丢掉被截断的首行
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def observer_rows(lae: Path) -> list[tuple[int, set[str]]]:
    rows = []
    for r in _tail_jsonl(lae / "l10_observer.jsonl"):
        tick = r.get("tick")
        prof = r.get("profile") or {}
        if isinstance(tick, int):
            rows.append((tick, {str(t) for t in prof.get("dead_types") or []}))
    rows.sort(key=lambda x: x[0])
    return rows


def alive_rows(rows: list[tuple[int, set[str]]], typ: str, lo: int, hi: int) -> int:
    """(lo, hi] 里该类型"没被算死"的行数。"""
    return sum(1 for tick, dead in rows if lo < tick <= hi and typ not in dead)


def shadow_families(lae: Path) -> dict[str, dict]:
    """注册表来源的 shadow 候选（同族取最新一条）。内置族的 shadow 不归这里判。"""
    fams: dict[str, dict] = {}
    for r in _tail_jsonl(lae / "l10_l5_proposals.jsonl"):
        if r.get("status") != "shadow_only":
            continue
        cand = r.get("candidate") or {}
        strat = cand.get("strategy") or {}
        prov = strat.get("provenance") or {}
        if prov.get("source") != "family_registry":
            continue
        sp = r.get("shadow_param") or {}
        name = strat.get("type")
        if not name or "key" not in sp:
            continue
        fams[str(name)] = {"family": str(name), "key": sp["key"], "value": sp["value"],
                           "tick": r.get("tick"), "registry_index": prov.get("registry_index")}
    return fams


def _whitelist() -> tuple[set[str], dict[str, tuple[float, float]]]:
    """复用桥里那份唯一真源（它从 tl_ego_server.py 的 PARAM_WHITELIST 解析）。"""
    spec_py = Path(__file__).resolve().parent / "l10" / "l10_l5_patch_bridge.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("_judge_bridge", str(spec_py))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_judge_bridge"] = mod
    spec.loader.exec_module(mod)
    return mod._whitelist_and_ranges()


# ── 动作 ────────────────────────────────────────────────────────────────────
def post_param(key: str, value, reason: str, ttl_seconds: float = 0.0) -> dict:
    body = json.dumps({"key": key, "value": value, "reason": reason,
                       "source": SRC, "ttl_seconds": ttl_seconds}).encode("utf-8")
    req = urllib.request.Request(API, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _load_state(lae: Path) -> dict:
    p = lae / "l10_family_judge_state.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(st, dict):
            st.setdefault("open", None)
            st.setdefault("history", [])
            return st
    except Exception:
        pass
    return {"open": None, "history": []}


def _save_state(lae: Path, st: dict) -> None:
    st["history"] = st.get("history", [])[-50:]
    p = lae / "l10_family_judge_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def _verdict(lae: Path, rec: dict) -> None:
    p = lae / "l10_family_verdicts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── 主流程 ──────────────────────────────────────────────────────────────────
def run(lae: Path = LAE, post=post_param, now: float | None = None) -> dict:
    st = _load_state(lae)
    rows = observer_rows(lae)
    if not rows:
        return {"action": "idle", "reason": "no observer rows"}
    tick = rows[-1][0]
    open_exp = st.get("open")

    # 1) 有在跑的实验 → 到窗口就判
    if open_exp:
        t0 = int(open_exp["t0_tick"])
        if tick < t0 + WINDOW:
            return {"action": "running", "family": open_exp["family"],
                    "tick": tick, "left": t0 + WINDOW - tick}
        typ = open_exp["dead_type"]
        alive = alive_rows(rows, typ, t0 + LAG, t0 + WINDOW)
        keep = alive >= MIN_ALIVE_ROWS
        rec = {**{k: open_exp.get(k) for k in ("family", "key", "value", "old_value",
                                               "dead_type", "t0_tick", "registry_file")},
               "judged_tick": tick, "alive_rows": alive, "baseline_alive": open_exp.get("baseline_alive"),
               "verdict": "keep" if keep else "kill", "policy": f"alive>={MIN_ALIVE_ROWS} in ({LAG},{WINDOW}]"}
        if not keep:
            old = open_exp.get("old_value")
            if old not in (None, "", "unset"):
                post(open_exp["key"], old, f"l10_family_judge kill {open_exp['family']}")
            rf = open_exp.get("registry_file")
            if rf:
                try:
                    (lae / "l10_family_registry" / rf).unlink()
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    rec["kill_note"] = f"registry file not removed: {exc}"
        _verdict(lae, {**rec, "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        st["history"].append(rec)
        st["open"] = None
        _save_state(lae, st)
        return {"action": rec["verdict"], **rec}

    # 2) 没在跑 → 挑一个够格开实验的
    fams = shadow_families(lae)
    if not fams:
        return {"action": "idle", "reason": "no registry-sourced shadow candidates"}
    wl, rng = _whitelist()
    current_dead = rows[-1][1]
    tried = {h.get("family") for h in st.get("history", [])}
    for name, f in fams.items():
        if name in tried:
            continue
        typ = _family_dead_type(lae, f)
        if not typ:
            continue
        if typ not in current_dead:
            continue
        lo, hi = rng.get(f["key"], (None, None))
        if f["key"] not in wl or lo is None or not (lo <= float(f["value"]) <= hi):
            continue
        baseline = alive_rows(rows, typ, tick - WINDOW, tick)
        if baseline >= MIN_ALIVE_ROWS:
            continue                      # 基线就活着 = 不算"被救活"
        exp = {"family": name, "key": f["key"], "value": float(f["value"]),
               "dead_type": typ, "t0_tick": tick, "baseline_alive": baseline,
               "registry_file": str(f.get("registry_index") or f"{name}.json").split("#")[0],
               "started_ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            res = post(f["key"], float(f["value"]),
                       f"l10_family_judge start {name} (dead_type={typ})", TTL_S)
        except Exception as exc:
            return {"action": "error", "family": name, "reason": f"apply failed: {exc}"}
        if str(res.get("status") or "").lower() != "ok":
            exp["rejected"] = res
            st["history"].append({**exp, "verdict": "rejected"})
            _save_state(lae, st)
            return {"action": "rejected", **exp}
        exp["old_value"] = res.get("old_value")
        st["open"] = exp
        _save_state(lae, st)
        return {"action": "started", **exp}
    return {"action": "idle", "reason": "no eligible family (dead type / whitelist / baseline)"}


def _family_dead_type(lae: Path, fam: dict) -> str | None:
    """族行里声明的目标死类型（when.dead_types_any[0]）。行没了就返回 None。"""
    rf = lae / "l10_family_registry" / f"{fam['family']}.json"
    try:
        row = json.loads(rf.read_text(encoding="utf-8"))
    except Exception:
        return None
    wanted = ((row.get("when") or {}).get("dead_types_any") or [])
    return str(wanted[0]) if wanted else None


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
