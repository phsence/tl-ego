"""l10_l5_patch_bridge.py — 把 L10 生成器的新族候选 (algorithm_selection + DSL) 变成
L5 通道能吃的 repair.json 提案。**只写提案, 绝不 apply。**

链路:
    l10_meta_algorithm_generator  (closed_self_reflection -> algorithm_selection + dsl)
      -> l10_dsl_compiler.compile_dsl        (提案时重编译: 锚点=当前文件, 天然新鲜)
        -> 本模块: 校验 -> ego_sandbox/l10g-<candidate_id>/repair.json
          -> scripts/l5_apply_repair.ps1     (Gate1 人工审查 -> Gate2 备份+apply+py_compile
                                              -> Gate3 重启+验证)

校验 (任一不过 -> 只记拒绝理由, 不落提案):
    1. append-only   new_text 必须以 old_text 开头 (只追加, 绝不改已有代码)
    2. 锚点新鲜      old_text.rstrip() == 目标文件当前内容.rstrip()
    3. 目标受限      patch.file 在 projects/tl-ego/ 之下且文件存在
    4. 禁区         追加块不得触碰信任边界 (审批/签名/验证器/网络策略/完整性守卫…)
    5. 大小上限     追加块 <= 20000 字符
    6. 可编译        new_text 写临时文件后 py_compile 通过

用法:
    H:/unsloth_env/Scripts/python.exe l10/l10_l5_patch_bridge.py
"""

from __future__ import annotations

import hashlib
import json
import py_compile
import re
import sys
import tempfile
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent          # projects/tl-ego/l10
TL_EGO_DIR = PROJECT_DIR.parent                        # projects/tl-ego
WORKSPACE = TL_EGO_DIR.parent.parent                   # workspace
LAE = WORKSPACE / ".lae"

sys.path.insert(0, str(PROJECT_DIR))
from l10_dsl_compiler import compile_dsl  # noqa: E402

GEN_LOG = LAE / "l10_meta_algorithm_generator.jsonl"
SANDBOX_ROOT = TL_EGO_DIR / "ego_sandbox"
PROPOSAL_LOG = LAE / "l10_l5_proposals.jsonl"
ALLOWED_ROOT = TL_EGO_DIR
MAX_APPEND_CHARS = 20_000

# 禁区: 追加块里出现这些 = 触碰信任边界/治理面, 提案直接拒 (白名单之外一律人来)
FORBIDDEN_PATTERNS = (
    "requires_human_approval", "capability_evolution_gate", "verify_evaluator_verdict",
    "evaluator_signing", "signing_key", "integrity_guard", "_NOT_LANDED_MARKERS",
    "architect_human_approval", "network_policy", "allowed_domains",
)


def _append_proposal_log(record: dict) -> None:
    try:
        with open(PROPOSAL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def latest_algorithm_selection(log_path: Path = GEN_LOG) -> dict | None:
    """宽容解析拼接 JSON 日志, 取最新 triggered 记录里第一个带 dsl 的候选。"""
    try:
        raw = log_path.read_text(encoding="utf-8")
    except Exception:
        return None
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
                last = obj
        except json.JSONDecodeError:
            nl = raw.find("\n", idx)
            if nl == -1:
                break
            idx = nl + 1
    if not last:
        return None
    for c in last.get("algorithm_candidates", []):
        if c.get("dsl"):
            return {"candidate": c, "tick": last.get("tick"), "record_ts": last.get("timestamp")}
    return None


def latest_triggered_candidate(log_path: Path = GEN_LOG) -> dict | None:
    """最新 triggered 记录里的第一个候选, **不论有没有 dsl**。

    2026-09-29 之后生成器换了形状: 候选带 strategy/code_anchor, 不再带 dsl。
    latest_algorithm_selection() 只认 dsl → 旧路径对新族恒返回 None → 通道看起来
    "在跑"(rc=0) 实际零产出。这里给新族留入口。
    """
    try:
        raw = log_path.read_text(encoding="utf-8")
    except Exception:
        return None
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
                last = obj
        except json.JSONDecodeError:
            nl = raw.find("\n", idx)
            if nl == -1:
                break
            idx = nl + 1
    if not last:
        return None
    cands = last.get("algorithm_candidates") or []
    if not cands:
        return None
    # 2026-11-10: 带上全部候选 + profile(死类型数)。旧形状只返回 cands[0],
    # 排后面的新族候选(TypeBudgetKWTA)连日志都不留; profile 供 _signals_snapshot。
    return {"candidate": cands[0], "candidates": cands, "profile": last.get("profile") or {},
            "tick": last.get("tick"), "record_ts": last.get("timestamp")}


# ── 2026-10-15 参数面通道: 新形状候选 -> 白名单参数处方 ──────────────────────
# 这一族候选(扩池/抬死类型)是**参数意图**, 不是代码补丁。它们此前唯一的去处是
# append-only 代码提案 → 追加即死代码(appended_shadow_unwired 81 条), 且没有 dsl
# 时连提案都写不出。落地形式改成参数面: 键必须出自 tl_ego_server.PARAM_WHITELIST
# (单一真相源), 值取候选自己写的数, 再按白名单范围 clamp —— 不发明数字, clamp 是
# self_model_evolution 的既有惯例; 应用/审计/重启重放由 _apply_runtime_param 负责。
SERVER_PY = TL_EGO_DIR / "tl_ego_server.py"

# 每种策略意图认可的数形式。kwta_pool_mult = 池相对基线的倍数(默认 1.0 = 200),
# 候选写的 n_active*X 是被当成"扩池意愿倍数"用的(语义在 tl_ego_server.py 消费点注明),
# 认不出这种形式就 unmappable 留日志 —— 绝不拿绝对值(min(2400,...))冒充倍数。
STRATEGY_PARAM_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "kwta_threshold_expansion": ("kwta_pool_mult", (r"n_active\s*\*\s*([\d.]+)",)),
    # 2026-11-10: 该族的真实形状是 "dead-type booster floor = 1.8" (实测
    # l10-algo-301146 s2_value), 不是 n_active*X —— 抄错族 => 4 次取数失败。
    "dead_type_booster_floor": ("kwta_pool_mult", (r"floor\s*=\s*([\d.]+)",)),
    # 2026-11-10 (L2 打通): 生成器新族 TypeBudgetKWTA 的决策 = 显式类型预算值。
    # 落点是已存在的运行时参数(白名单内, 消费点 server 4708/11690), 不是代码追加 ——
    # 代码路线对它恒判 appended_shadow_unwired(签名作为死代码躺在 server 23764 行)。
    # patterns 为空: 值走 strategy.dsl.params.type_budget(真实数字), 见 _value_from_strategy。
    "algorithm_selection": ("type_budget_kwta_budget", ()),
}

_RANGES_CACHE: dict[str, tuple[float, float]] = {}
_WHITELIST_CACHE: set[str] = set()


def _whitelist_and_ranges() -> tuple[set[str], dict[str, tuple[float, float]]]:
    """从 tl_ego_server.py 源码正则读白名单/范围。不 import: 导入有单实例 sys.exit 副作用。"""
    global _WHITELIST_CACHE, _RANGES_CACHE
    if _WHITELIST_CACHE:
        return _WHITELIST_CACHE, _RANGES_CACHE
    try:
        txt = SERVER_PY.read_text(encoding="utf-8", errors="replace")
        block = re.search(r"PARAM_WHITELIST\s*=\s*\{(.*?)\n\}", txt, re.S)
        if block:
            _WHITELIST_CACHE = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', block.group(1)))
        for k, lo, hi in re.findall(
                r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*\)', txt):
            _RANGES_CACHE[k] = (float(lo), float(hi))
    except Exception:
        pass
    return _WHITELIST_CACHE, _RANGES_CACHE


def _value_from_strategy(strategy: dict, patterns: tuple[str, ...]) -> float | None:
    """按该意图认可的形式取数(s2 优先 = 扩展侧提案值)。取不到就 None(不猜)。"""
    # 2026-11-10: 新族(TypeBudgetKWTA)把数放在 dsl 里(真实数字, 不是文本正则)。
    # 实测形状(生成器 log tick 70103): dsl.algorithm.name/params.type_budget;
    # 兼容扁平 dsl.name/params。只认 name=TypeBudgetKWTA; 没有/不认就 None, 不猜。
    dsl = strategy.get("dsl") or {}
    if isinstance(dsl, dict):
        algo = dsl.get("algorithm") if isinstance(dsl.get("algorithm"), dict) else dsl
        if algo.get("name") == "TypeBudgetKWTA":
            try:
                return float((algo.get("params") or {})["type_budget"])
            except (KeyError, TypeError, ValueError):
                return None
    for field in ("s2_value", "hypothesis", "change_description", "s1_value", "target"):
        txt = str(strategy.get(field) or "")
        for pat in patterns:
            m = re.search(pat, txt)
            if m:
                return float(m.group(1))
    return None


def _signals_snapshot(found: dict) -> dict:
    """闭环评价三信号(2026-11-10 用户点名): 死类型数(生成器输入)、签名冲突率
    (提案日志 appended_shadow_unwired 占比)、新族产出率(本轮 distinct 策略族)。
    只读快照, 拿不到就缺项 —— 不猜。"""
    sig: dict = {}
    prof = found.get("profile") or {}
    if prof.get("dead_type_count") is not None:
        sig["dead_type_count"] = prof["dead_type_count"]
    try:
        from collections import Counter
        st: Counter = Counter()
        with open(PROPOSAL_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 262144))
            for line in f.read().decode("utf-8", "replace").splitlines():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                st[str(r.get("status"))] += 1
        tot = sum(st.values())
        if tot:
            sig["conflict_rate"] = round(st.get("appended_shadow_unwired", 0) / tot, 4)
            sig["proposal_total"] = tot
    except Exception:
        pass
    fams = {str((c.get("strategy") or {}).get("type") or "?")
            for c in (found.get("candidates") or [])}
    if fams:
        sig["family_yield"] = len(fams)
    return sig


def _propose_one(cand: dict, tick, signals: dict) -> bool:
    """单个候选 -> 参数处方。返回是否落了 param_proposed 行。"""
    if not cand:
        return False
    cid = str(cand.get("candidate_id") or "unknown")
    strategy = cand.get("strategy") or {}
    stype = str(strategy.get("type") or "")
    wl, rng = _whitelist_and_ranges()
    # Shadow-only strategies have a live consumer and a measured value, but must
    # not enter the runtime applier. Keep them auditable without a `param` key;
    # _fresh_param_proposals only accepts status=param_proposed.
    if strategy.get("shadow_only"):
        shadow = strategy.get("shadow_param") or {}
        shadow_key = str(shadow.get("key") or "")
        try:
            shadow_value = float(shadow["value"])
        except (KeyError, TypeError, ValueError):
            shadow_key, shadow_value = "", None
        if not shadow_key or shadow_value is None or shadow_key not in wl:
            _append_proposal_log({"ts": time.time(), "candidate_id": cid, "tick": tick,
                                  "status": "param_unmappable", "signals": signals,
                                  "reason": "shadow candidate missing a valid whitelisted shadow_param"})
            return False
        lo, hi = rng.get(shadow_key, (0.0, 1e9))
        value = min(max(shadow_value, lo), hi)
        _append_proposal_log({"ts": time.time(), "candidate_id": cid, "tick": tick,
                              "status": "shadow_only", "signals": signals,
                              "candidate": cand,
                              "candidate_origin": cand.get("provenance") or {"source": "l10_generator"},
                              "shadow_param": {"key": shadow_key, "value": value,
                                               "requested": shadow_value},
                              "reason": "candidate recorded for evaluation; runtime apply disabled"})
        print(f"[l10-l5] shadow candidate {shadow_key}={value:g}; runtime apply disabled")
        return True

    mapped = STRATEGY_PARAM_MAP.get(stype)
    key, patterns = mapped if mapped else (None, ())
    if not key or key not in wl:
        _append_proposal_log({"ts": time.time(), "candidate_id": cid, "tick": tick,
                              "status": "param_unmappable", "signals": signals,
                              "reason": f"strategy '{stype}' has no whitelisted landing pad "
                                        f"(map={key}, whitelist={len(wl)} keys)"})
        print(f"[l10-l5] strategy '{stype}' -> no landing pad; logged (not silently dropped)")
        return False
    val_raw = _value_from_strategy(strategy, patterns)
    if val_raw is None:
        _append_proposal_log({"ts": time.time(), "candidate_id": cid, "tick": tick,
                              "status": "param_unmappable", "signals": signals,
                              "reason": f"strategy '{stype}' carries no value in an accepted form "
                                        f"{patterns}; refusing to invent one"})
        print(f"[l10-l5] strategy '{stype}' -> no usable number; logged")
        return False
    lo, hi = rng.get(key, (0.0, 1e9))
    val = min(max(val_raw, lo), hi)
    note = (f"requested {val_raw:g} -> clamped to {val:g} (range {lo:g}..{hi:g})"
            if val != val_raw else f"{val:g} (in range {lo:g}..{hi:g})")
    _append_proposal_log({"ts": time.time(), "candidate_id": cid, "tick": tick,
                          "status": "param_proposed", "signals": signals,
                          "param": {"key": key, "value": val, "requested": val_raw,
                                    "reason": f"L10 {stype}: {note}"},
                          "reason": note})
    print(f"[l10-l5] param proposal: {key}={val:g} — {note} (candidate {cid})")
    if key == "type_budget_kwta_budget":
        # 预算写在 enable 门后面是死写(消费点先判 enable) —— 成对写, 与 ego 自己
        # 10-24/10-29 的 enable+budget 配对惯例一致。
        _append_proposal_log({"ts": time.time(), "candidate_id": cid, "tick": tick,
                              "status": "param_proposed", "signals": signals,
                              "param": {"key": "type_budget_kwta_enable", "value": 1.0,
                                        "requested": 1.0,
                                        "reason": "L10 algorithm_selection: enable pair for "
                                                  "type_budget_kwta_budget (budget without "
                                                  "enable is a dead write)"},
                              "reason": "enable pair for type_budget_kwta_budget"})
        print(f"[l10-l5] param proposal: type_budget_kwta_enable=1 — enable pair (candidate {cid})")
    return True


def propose_param(found: dict) -> int:
    """新形状候选 -> 参数处方(写进同一个提案日志, 状态 param_proposed)。

    2026-11-10: 遍历全部候选(旧版只取 cands[0] —— 生成器新族 TypeBudgetKWTA
    通常排在第 2 位以后, 被静默丢弃; 198 条 param_unmappable 有一部分就是这么来的)。
    返回落了 param_proposed 行的候选数(供 main() 决定是否还要走代码路线)。
    """
    cands = found.get("candidates") or ([found["candidate"]] if found.get("candidate") else [])
    if not cands:
        print("[l10-l5] no triggered candidate in latest generator record")
        return 0
    tick = found.get("tick")
    signals = _signals_snapshot(found)
    mapped = 0
    for cand in cands:
        if _propose_one(cand, tick, signals):
            mapped += 1
    return mapped


def validate_patch(patch: dict, sandbox_root: Path = SANDBOX_ROOT) -> tuple[bool, str]:
    """(ok, reason)。检查顺序 = 越便宜的越先。"""
    old = str(patch.get("old_text") or "")
    new = str(patch.get("new_text") or "")
    rel = str(patch.get("file") or "")
    if not old or not new:
        return False, "empty old_text/new_text"
    if not new.startswith(old):
        return False, "not append-only: new_text does not start with old_text"
    appended = new[len(old):]
    if len(appended) > MAX_APPEND_CHARS:
        return False, f"append too large ({len(appended)} chars > {MAX_APPEND_CHARS})"

    target = (WORKSPACE / rel).resolve()
    if ALLOWED_ROOT not in target.parents:
        return False, f"target outside projects/tl-ego: {rel}"
    if not target.is_file():
        return False, f"target not found: {rel}"
    # 锚点=文件尾窗口(或整文件): 必须仍是目标文件的尾部, 且全文唯一
    # (ps1 用 Contains/替换首处命中; 不唯一就可能改错地方)
    target_text = target.read_text(encoding="utf-8")
    if not target_text.endswith(old) or target_text.count(old) != 1:
        return False, "stale anchor: target tail changed or anchor not unique (recompile needed)"

    for pat in FORBIDDEN_PATTERNS:
        if pat in appended:
            return False, f"forbidden zone in appended code: {pat}"

    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            tmp = Path(td) / "patched_target.py"
            # 编译"打上补丁后的整个目标文件", 不是补丁片段: 窄锚点是从行中间截的窗口,
            # 单独编译它必然 SyntaxError (与 ps1 的替换语义保持一致: 替换首次命中)
            tmp.write_text(target_text.replace(old, new), encoding="utf-8")
            py_compile.compile(str(tmp), cfile=str(Path(td) / "out.pyc"), doraise=True)
    except py_compile.PyCompileError as e:
        return False, f"patched result does not compile: {str(e)[:200]}"
    return True, "ok"


def write_proposal(compiled: dict, *, candidate: dict, tick: int | None,
                   sandbox_root: Path = SANDBOX_ROOT) -> dict:
    patch = compiled["patch"]
    cid = str(candidate.get("candidate_id") or "unknown")
    safe_cid = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cid)
    sandbox = sandbox_root / f"l10g-{safe_cid}"
    sandbox.mkdir(parents=True, exist_ok=True)
    repair = {
        "goal_id": f"l10g-{safe_cid}",
        "tick": tick,
        "diagnosis": (f"L10 换族候选: 向 {patch['file']} 追加 {compiled['algorithm_name']} "
                      f"(append-only 插入, 无调用点 = 惰性; 是否接线由 Gate1 审查决定)"),
        "patches": [{
            "file": patch["file"],
            "description": patch.get("description", "L10 DSL insertion"),
            "operation": "replace",
            "old_text": patch["old_text"],
            "new_text": patch["new_text"],
        }],
        "restart_required": False,   # 纯追加、无调用点: 不重启也不改行为
        "verification": {"compile_check": True, "status_check": True, "observe_ticks": 3},
        "provenance": {
            "l10_candidate_id": cid,
            "algorithm_name": compiled["algorithm_name"],
            "dsl": compiled.get("dsl"),
            "compiled_at": compiled.get("compiled_at"),
            "append_sha256": hashlib.sha256(
                patch["new_text"][len(patch["old_text"]):].encode("utf-8")).hexdigest()[:16],
            "proposed_via": "l10_l5_patch_bridge.py",
        },
    }
    out = sandbox / "repair.json"
    out.write_text(json.dumps(repair, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"sandbox": str(sandbox), "repair_json": str(out)}


def main() -> int:
    # 2026-11-10 (L2 打通): 参数路由优先, 且遍历全部候选。新族 TypeBudgetKWTA
    # 候选带顶层 dsl, 旧顺序先走代码路线 -> 签名已作为死代码躺在 server 里 ->
    # appended_shadow_unwired, 参数落点(白名单内的 type_budget_kwta_budget)永远
    # 够不着。有候选落了参数行就不再走代码路线(代码路线对已落地参数是死路);
    # 没落(全部 unmappable)也**不重跑** —— 候选已记过日志, 重跑=重复行。
    found_all = latest_triggered_candidate()
    if not found_all:
        print("[l10-l5] no triggered candidate in latest generator record")
        return 0
    if propose_param(found_all):
        return 0
    found = latest_algorithm_selection()
    if not found:
        return 0
    candidate, tick = found["candidate"], found.get("tick")
    cid = candidate.get("candidate_id")
    try:
        compiled = compile_dsl(candidate["dsl"])
    except Exception as e:
        _append_proposal_log({"ts": time.time(), "candidate_id": cid, "status": "rejected",
                              "reason": f"compile failed: {e}"})
        print(f"[l10-l5] compile failed: {e}")
        return 0

    patch = compiled.get("patch")
    if patch is None:
        # 2026-09-20: 状态码改正。旧文案 "already_applied" 是谎——`patch=None`
        # 只说明该算法签名的代码块已追加进目标文件(见 _build_insertion_hunk
        # 的签名去重)，不代表它被调用、被执行、有效果。实测 114 条
        # already_applied 里，TypeBudgetKWTA 躺在 tl_ego_server.py 23764 行
        # 零调用点，纯死代码。区分三种结局，别让"追加过"伪装成"生效了"。
        _append_proposal_log({"ts": time.time(), "candidate_id": cid,
                              "status": "appended_shadow_unwired",
                              "reason": "algorithm signature already present in target file "
                                        "as dead code (no call site) — append happened, "
                                        "wiring never did"})
        print("[l10-l5] signature present but UNWIRED -> nothing to propose "
              "(was mislabeled already_applied)", flush=True)
        return 0

    ok, reason = validate_patch(patch)
    if not ok:
        _append_proposal_log({"ts": time.time(), "candidate_id": cid, "tick": tick,
                              "status": "rejected", "reason": reason})
        print(f"[l10-l5] rejected: {reason}")
        return 0

    written = write_proposal(compiled, candidate=candidate, tick=tick)
    _append_proposal_log({"ts": time.time(), "candidate_id": cid, "tick": tick,
                          "status": "proposed", "reason": reason, **written})
    print(f"[l10-l5] proposal written: {written['repair_json']}")
    print(f"[l10-l5] review:  pwsh scripts/l5_apply_repair.ps1 -SandboxDir {written['sandbox']} -DryRun")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
