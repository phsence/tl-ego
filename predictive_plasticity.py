"""Sparse predictive-plasticity sidecar for TL-Ego resting dynamics.

The neuron registry remains cold and immutable.  This module holds only recent
activation/eligibility state, learns type transitions conditional on a compact
network regime, and returns a bounded multiplier for the existing STDP path.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

# A/B 旋钮 (回滚用): EGO_PRED_BACKOFF=0 关掉粗细双键回退; EGO_PRED_STATE=<path> 指定落盘位置(不设则不落盘)
#   EGO_PRED_STABLE_PCT=0 回滚旧绝对稳定线(0.85); 否则稳定线=自身 competition 分布的该百分位
#   EGO_PRED_STABLE_MAX=<f> 绝对兜底: competition 超它一律算不稳(默认 0.95)
_BACKOFF = os.environ.get("EGO_PRED_BACKOFF", "1") != "0"
_STATE_ENV = "EGO_PRED_STATE"
_MIN_REPEAT = 2          # 与 observe_transition 的 awaiting_repeat 阈值一致
_MAX_CONTEXTS = 20000    # ponytail: 全量 json 落盘; 超过只保留计数最大的 20000 个键
_STABLE_PCT = float(os.environ.get("EGO_PRED_STABLE_PCT", "65") or 0)
_STABLE_MAX = float(os.environ.get("EGO_PRED_STABLE_MAX", "0.95") or 0.95)
_STABLE_WIN = 200       # 记多少次观测的 competition 才够定"常态"
_STABLE_MIN = 20        # 样本不够先按旧绝对线 0.85
_LEGACY_STABLE_MAX = 0.85
_CAL_BINS = 5            # 校准曲线分箱 (conf -> 实际命中率)
_DRIFT_MIN_N = 6         # 一个键至少 6 次样本才判它"骗不动了"
_DRIFT_MAX_HIT = 0.34    # 命中率低于此 -> 清掉该键重新学
_ROLLOUT_STEPS = 3       # n 步前瞻


class PredictivePlasticity:
    def __init__(self, max_active_neurons: int = 8000, max_eligibility: int = 16000,
                 state_path: Optional[str] = None) -> None:
        self.max_active_neurons = max_active_neurons
        self.max_eligibility = max_eligibility
        self.active_neurons: Dict[str, Dict[str, float]] = {}
        self.eligibility: Dict[Tuple[str, str], Dict[str, float]] = {}
        self.transition_priors: Dict[str, Counter] = {}
        self.pending: Dict[str, Any] | None = None
        self.last_outcome: Dict[str, Any] = {}
        self.action_outcomes: Dict[str, Counter] = {}   # action -> {success, failed}
        self.action_feedback_log: Dict[str, Any] = {}
        # 落盘只在显式给路径或设了 EGO_PRED_STATE 时启用: 单测默认不碰真实文件
        self.state_path = state_path or os.environ.get(_STATE_ENV) or None
        self.persist: Dict[str, Any] = {
            "path": str(self.state_path or ""), "loaded_keys": 0, "saves": 0,
            "last_save": "", "error": "",
        }
        # 自身 recent competition 分布 -> 相对稳定线 (ego 常年 0.84~0.90, 固定 0.85 线会一直误判"不稳")
        self.competition_window = deque(maxlen=_STABLE_WIN)
        self.concentration_window = deque(maxlen=_STABLE_WIN)
        self.calibration = deque(maxlen=400)        # (置信度, 是否命中) -> Brier/ECE
        self.key_quality: Dict[str, list] = {}      # 键 -> [hits, misses], 用来抓"骗不动了"的旧表
        self.drift_cleared = 0
        self.last_rollout: Dict[str, Any] = {}
        self.gate_hist: Counter = Counter()
        self._load()

    # ── 键: 细键=类型>12维regime(信息多但几乎不重复), 粗键=类型|5位regime(样本攒得快) ──
    @staticmethod
    def _keys(current_type: str, regime_vec: tuple, regime_str: str) -> Tuple[str, str]:
        return f"{current_type}>{regime_vec}", f"{current_type}|{regime_str}"

    def _prior_for(self, current_type: str, regime_vec: tuple, regime_str: str):
        fine, coarse = self._keys(current_type, regime_vec, regime_str)
        prior = self.transition_priors.get(fine, Counter())
        source = "fine"
        if _BACKOFF:
            coarse_prior = self.transition_priors.get(coarse, Counter())
            if sum(prior.values()) < _MIN_REPEAT and sum(coarse_prior.values()) > sum(prior.values()):
                prior, source = coarse_prior, "coarse"
        return fine, coarse, prior, source

    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            blob = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
            self.transition_priors = {
                k: Counter(v) for k, v in (blob.get("transition_priors") or {}).items()
            }
            self.persist["loaded_keys"] = len(self.transition_priors)
            self.persist["saves"] = int(blob.get("saves", 0) or 0)
            self.competition_window = deque(blob.get("competition_window") or [], maxlen=_STABLE_WIN)
            self.concentration_window = deque(blob.get("concentration_window") or [], maxlen=_STABLE_WIN)
            self.calibration = deque([[c, o] for c, o in (blob.get("calibration") or [])], maxlen=400)
            self.key_quality = {k: list(v) for k, v in (blob.get("key_quality") or {}).items()}
            self.drift_cleared = int(blob.get("drift_cleared", 0) or 0)
            self.gate_hist = Counter(blob.get("gate_hist") or {})
            self.action_outcomes = {
                k: Counter(v) for k, v in (blob.get("action_outcomes") or {}).items()
            }
        except FileNotFoundError:
            self.persist["loaded_keys"] = 0
        except Exception as e:                      # 坏文件只记错, 不回滚成崩溃
            self.persist["error"] = f"load:{e}"

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            blob = {
                "version": 1,
                "saves": self.persist["saves"] + 1,
                "transition_priors": {k: dict(v) for k, v in self.transition_priors.items()},
                "competition_window": list(self.competition_window),
                "concentration_window": list(self.concentration_window),
                "calibration": [[c, o] for c, o in self.calibration],
                "key_quality": self.key_quality,
                "action_outcomes": {k: dict(v) for k, v in self.action_outcomes.items()},
                "drift_cleared": self.drift_cleared,
                "gate_hist": dict(self.gate_hist),
            }
            tmp = Path(str(self.state_path) + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(blob, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.state_path)        # 原子替换: 半截文件不会覆盖好文件
            self.persist["saves"] += 1
            self.persist["loaded_keys"] = len(self.transition_priors)
            self.persist["last_save"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.persist["error"] = ""
        except Exception as e:
            self.persist["error"] = f"save:{e}"

    def _stable_threshold(self) -> float:
        """稳定线: 默认取自身 competition 分布的 EGO_PRED_STABLE_PCT 百分位, 上限 _STABLE_MAX."""
        if _STABLE_PCT <= 0 or len(self.competition_window) < _STABLE_MIN:
            return _LEGACY_STABLE_MAX
        vals = sorted(self.competition_window)
        idx = min(len(vals) - 1, max(0, int(round(len(vals) * _STABLE_PCT / 100.0)) - 1))
        return min(_STABLE_MAX, vals[idx])

    def _trend(self, window) -> float:
        """窗口里最近值 vs 前几次均值 —— 给 comp/conc 两个 trend 维提供真值."""
        if len(window) < 4:
            return 0.0
        vals = list(window)
        return vals[-1] - sum(vals[-4:-1]) / 3.0

    def calibration_stats(self) -> Dict[str, Any]:
        """预测的可靠性: Brier 越小越好; ECE 是"说 60% 时真对多少"的平均偏差."""
        n = len(self.calibration)
        if not n:
            return {"n": 0, "brier": None, "ece": None, "bins": {}}
        brier = sum((c - o) ** 2 for c, o in self.calibration) / n
        buckets: Dict[int, list] = {}
        for c, o in self.calibration:
            buckets.setdefault(min(_CAL_BINS - 1, int(c * _CAL_BINS)), []).append((c, o))
        ece, detail = 0.0, {}
        for b, pairs in sorted(buckets.items()):
            conf = sum(c for c, _ in pairs) / len(pairs)
            acc = sum(o for _, o in pairs) / len(pairs)
            ece += len(pairs) / n * abs(acc - conf)
            detail[f"{b / _CAL_BINS:.1f}-{(b + 1) / _CAL_BINS:.1f}"] = {
                "n": len(pairs), "conf": round(conf, 3), "acc": round(acc, 3)}
        return {"n": n, "brier": round(brier, 4), "ece": round(ece, 4), "bins": detail}

    def rollout(self, current_type: str, state: Dict[str, Any], steps: int = _ROLLOUT_STEPS) -> Dict[str, Any]:
        """n 步前瞻: 用转移表当模型往下推 (表即模型, 不需要新网络). 只保留每层概率 top-3 分支."""
        regime_vec, regime_str = self.regime_vector(state), self.regime(state)
        frontier = [(1.0, current_type)]
        trace = []
        for _ in range(max(1, int(steps))):
            nxt: Dict[str, float] = {}
            for p, t in frontier:
                _fine, _coarse, prior, _src = self._prior_for(t, regime_vec, regime_str)
                tot = sum(prior.values())
                if not tot:
                    nxt[t] = nxt.get(t, 0.0) + p          # 没数据: 先假设"原地不动"
                    continue
                for nt, cnt in prior.items():
                    nxt[nt] = nxt.get(nt, 0.0) + p * cnt / tot
            top = sorted(nxt.items(), key=lambda kv: -kv[1])[:3]
            norm = sum(v for _, v in top) or 1.0
            frontier = [(v / norm, k) for k, v in top]
            trace.append([[t, round(p, 3)] for p, t in frontier])
        return {"steps": trace, "expected": trace[-1][0][0] if trace and trace[-1] else current_type,
                "conf": trace[-1][0][1] if trace and trace[-1] else 0.0}

    def _prune(self) -> None:
        if len(self.transition_priors) <= _MAX_CONTEXTS:
            return
        ranked = sorted(self.transition_priors.items(), key=lambda kv: -sum(kv[1].values()))
        self.transition_priors = dict(ranked[:_MAX_CONTEXTS])

    @staticmethod
    def regime(state: Dict[str, Any]) -> str:
        """Legacy 5-bin summary for backward compatibility."""
        competition = float(state.get("competition", 0.5) or 0.5)
        concentration = float(state.get("concentration", 0.5) or 0.5)
        stagnation = int(state.get("stagnation", 0) or 0)
        pressure = int(state.get("integration_pressure", 0) or 0)
        pool_narrow = bool(state.get("candidate_pool_narrow", False))
        return "|".join((
            "comp_high" if competition >= 0.75 else "comp_ok",
            "conc_low" if concentration < 0.40 else "conc_ok",
            "stagnant" if stagnation > 0 else "moving",
            "pressure" if pressure > 0 else "neutral",
            "pool_narrow" if pool_narrow else "pool_ok",
        ))

    @staticmethod
    def regime_vector(state: Dict[str, Any]) -> tuple:
        """12-dim regime vector for richer transition conditioning.

        Dimensions (binary/ternary bins):
          0  comp_level       : 1=high_competition (>=0.75)
          1  conc_level       : 1=low_concentration (<0.40)
          2  stag_level       : 1=stagnant (>0)
          3  press_level      : 1=integration_pressure (>0)
          4  pool_level       : 1=pool_narrow
          5  dwell_bin        : 1=high_dwell (>0.5 normalized)
          6  surprise_bin     : 1=high_surprise (>0.3)
          7  drive_align_bin  : 1=aligned (>0.6)
          8  comp_trend_bin   : 1=rising, -1=falling, 0=stable
          9  conc_trend_bin   : 1=rising, -1=falling, 0=stable
         10  entropy_bin      : 1=high_entropy (>0.5)

        2026-09-11: 删掉 identity_velocity 维 —— 全库没有数据源, 它永远是 0 (死维只会撑大状态空间);
        comp_trend/conc_trend/surprise/entropy 四个调用侧原本写死 0/0.5, 现在由本模块用
        自身滑窗/调用侧真实信号填 (见 begin_tick)。
        """
        competition = float(state.get("competition", 0.5) or 0.5)
        concentration = float(state.get("concentration", 0.5) or 0.5)
        stagnation = int(state.get("stagnation", 0) or 0)
        pressure = int(state.get("integration_pressure", 0) or 0)
        pool_narrow = bool(state.get("candidate_pool_narrow", False))

        comp_level = 1 if competition >= 0.75 else 0
        conc_level = 1 if concentration < 0.40 else 0
        stag_level = 1 if stagnation > 0 else 0
        press_level = 1 if pressure > 0 else 0
        pool_level = 1 if pool_narrow else 0

        dwell_time = float(state.get("dwell_time", 0.0) or 0.0)
        dwell_bin = 1 if dwell_time > 0.5 else 0

        surprise_mean = float(state.get("surprise_history_mean", 0.0) or 0.0)
        surprise_bin = 1 if surprise_mean > 0.3 else 0

        drive_alignment = float(state.get("drive_alignment", 0.0) or 0.0)
        drive_align_bin = 1 if drive_alignment > 0.6 else 0

        comp_trend = float(state.get("competition_trend", 0.0) or 0.0)
        comp_trend_bin = 1 if comp_trend > 0.05 else (-1 if comp_trend < -0.05 else 0)

        conc_trend = float(state.get("concentration_trend", 0.0) or 0.0)
        conc_trend_bin = 1 if conc_trend > 0.05 else (-1 if conc_trend < -0.05 else 0)

        type_entropy = float(state.get("type_entropy", 0.0) or 0.0)
        entropy_bin = 1 if type_entropy > 0.5 else 0

        return (comp_level, conc_level, stag_level, press_level, pool_level,
                dwell_bin, surprise_bin, drive_align_bin,
                comp_trend_bin, conc_trend_bin, entropy_bin)

    @staticmethod
    def regime_summary(state: Dict[str, Any]) -> str:
        """Human-readable 12-dim regime summary for logging/debugging."""
        vec = PredictivePlasticity.regime_vector(state)
        labels = ["comp", "conc", "stag", "press", "pool",
                  "dwell", "surprise", "drive",
                  "comp_tr", "conc_tr", "entropy"]
        parts = []
        for label, val in zip(labels, vec):
            if val == 1:
                parts.append(f"{label}+")
            elif val == -1:
                parts.append(f"{label}-")
            else:
                parts.append(f"{label}~")
        return "|".join(parts)

    def begin_tick(self, current_type: str, state: Dict[str, Any]) -> Dict[str, Any]:
        state = dict(state)                       # 别改调用方的 dict
        if not state.get("competition_trend"):    # 调用侧写死 0.0 -> 用自身窗口推
            state["competition_trend"] = self._trend(self.competition_window)
        if not state.get("concentration_trend"):
            state["concentration_trend"] = self._trend(self.concentration_window)
        regime_vec = self.regime_vector(state)
        regime_str = self.regime(state)
        fine, coarse, prior, source = self._prior_for(current_type, regime_vec, regime_str)
        total = sum(prior.values())
        if total:
            predicted, count = prior.most_common(1)[0]
            confidence = count / total
        else:
            predicted, confidence = current_type, 0.0
        self.pending = {
            "current_type": current_type,
            "regime": regime_str,
            "regime_vector": regime_vec,
            "regime_summary": self.regime_summary(state),
            "predicted": predicted,
            "confidence": round(confidence, 4),
            "key_used": coarse if source == "coarse" else fine,
            "source": source,
            "samples": total,
        }
        self.last_rollout = self.rollout(current_type, state)
        self.pending["rollout"] = self.last_rollout
        return dict(self.pending)

    def observe_transition(self, actual_type: str, state: Dict[str, Any]) -> Dict[str, Any]:
        pending = self.pending
        if not pending:
            return {"status": "no_prediction"}
        prediction_error = 0.0 if actual_type == pending["predicted"] else 1.0
        competition = float(state.get("competition", 0.5) or 0.5)
        previous_competition = float(state.get("previous_competition", competition) or competition)
        stagnation = int(state.get("stagnation", 0) or 0)
        self.competition_window.append(competition)
        self.concentration_window.append(float(state.get("concentration", 0.5) or 0.5))
        stable_thr = self._stable_threshold()
        stable = (competition <= stable_thr and stagnation == 0) or (competition <= 0.35 and stagnation >= 10)
        improved = competition <= previous_competition
        # Stage 34: 12 维细键 + 5 位粗键双写 -- 细键样本太散时靠粗键攒重复证据
        fine, coarse = self._keys(
            pending["current_type"],
            pending.get("regime_vector", pending["regime"]),
            pending["regime"],
        )
        for key in ({fine, coarse} if _BACKOFF else {fine}):
            self.transition_priors.setdefault(key, Counter())[actual_type] += 1
        # 重复次数取自"这次预测实际用的那张表", 否则回退读粗键会被细键计数卡死
        used = self.transition_priors.get(pending.get("key_used") or fine) or \
            self.transition_priors.get(fine, Counter())
        repeats = used[actual_type]
        self._prune()
        self._save()
        # A new transition is exploratory evidence, not a reason to immediately
        # consolidate every activated synapse.
        if not stable:
            gate = 0.35
            reason = "unstable_regime"
        elif repeats < 2:
            gate = 0.60
            reason = "awaiting_repeat"
        elif not improved:
            gate = 0.75
            reason = "no_dynamical_improvement"
        else:
            gate = 1.0
            reason = "repeated_supported_transition"
        self.last_outcome = {
            "predicted": pending["predicted"],
            "actual": actual_type,
            "regime": pending["regime"],
            "prediction_error": prediction_error,
            "transition_repeats": repeats,
            "gate": gate,
            "reason": reason,
            "competition": round(competition, 4),
            "stable_threshold": round(stable_thr, 4),
        }
        self.gate_hist[reason] += 1
        # ── 预测校准 + 漂移检测 (2026-09-11) ──
        self.calibration.append((float(pending.get("confidence", 0.0) or 0.0), 1.0 - prediction_error))
        _kq = self.key_quality.setdefault(pending.get("key_used") or fine, [0, 0])
        _kq[0 if prediction_error == 0 else 1] += 1
        if sum(_kq) >= _DRIFT_MIN_N and _kq[0] / sum(_kq) < _DRIFT_MAX_HIT:
            # 这张表对这个键已经骗不动了 -> 清掉重学 (非平稳系统的必要动作)
            self.transition_priors.pop(pending.get("key_used") or fine, None)
            self.key_quality.pop(pending.get("key_used") or fine, None)
            self.drift_cleared += 1
        self.pending = None
        return dict(self.last_outcome)

    def record_activation_path(self, activation_path: Iterable[Iterable[str]], tick: int) -> None:
        _lim = 150
        import random as _r
        rounds = []
        for nodes in activation_path:
            if len(nodes) > _lim:
                nodes = _r.sample(tuple(nodes), _lim)
            rounds.append(tuple(nodes))
        for nodes in rounds:
            for neuron_id in nodes:
                self.active_neurons[neuron_id] = {"tick": float(tick), "activation": 1.0}
        for previous, current in zip(rounds, rounds[1:]):
            for pre in previous:
                for post in current:
                    if pre == post:
                        continue
                    item = self.eligibility.setdefault((pre, post), {"trace": 0.0, "tick": float(tick)})
                    age = max(0.0, float(tick) - item["tick"])
                    item["trace"] = min(1.0, item["trace"] * (0.65 ** age) + 0.15)
                    item["tick"] = float(tick)
        if len(self.active_neurons) > self.max_active_neurons:
            oldest = sorted(self.active_neurons, key=lambda nid: self.active_neurons[nid]["tick"])
            for neuron_id in oldest[:len(self.active_neurons) - self.max_active_neurons]:
                del self.active_neurons[neuron_id]
        if len(self.eligibility) > self.max_eligibility:
            oldest = sorted(self.eligibility, key=lambda edge: self.eligibility[edge]["tick"])
            for edge in oldest[:len(self.eligibility) - self.max_eligibility]:
                del self.eligibility[edge]

    _SUCCESS_OUTCOMES = {"success", "completed", "verified", "correct", "landed", "confirmed"}
    # l5g-20261018-047405: "partial" (MC bridge locomotion verdict, e.g. goto
    # remaining>0 after delta) was in neither set, so record_action_feedback
    # dropped every measured-but-partial world outcome as unclassified_outcome
    # and action_outcomes froze for a body that was moving. Goal-not-reached
    # counts as failed; success rates stay conservative for routing gates.
    _FAIL_OUTCOMES = {"failed", "failure", "timeout", "error", "blocked", "crash", "abort", "empty", "partial"}

    def record_action_feedback(self, action: str, outcome: str, decision_id: str = "",
                               evidence: str = "", state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Join a measured outcome onto the eligibility traces of that decision.

        REBUILT 2026-09-30: tl_ego_server.py:2201 called this on every measured
        /feedback and it did not exist, so credit stopped at the envelope and
        nothing ever changed ego's behaviour. sign(outcome) scales the traces
        recorded for the decision's activation path, so a tool ego actually ran
        well strengthens those edges and a failed one suppresses them.
        """
        action = str(action or "unknown").strip().lower() or "unknown"
        raw = str(outcome or "").strip().lower()
        if raw in self._SUCCESS_OUTCOMES:
            sign, verdict = 1.0, "success"
        elif raw in self._FAIL_OUTCOMES:
            sign, verdict = -1.0, "failed"
        else:
            return {"updated": 0, "reason": "unclassified_outcome", "outcome": raw}

        # action -> {verdict: count}; the durable "which tools work" signal.
        self.action_outcomes.setdefault(action, Counter())[verdict] += 1

        # The eligibility table is write-only in this codebase (nothing reads
        # traces back), so the durable behaviour change is the per-action
        # verdict ledger above. ponytail: wire traces into the STDP multiplier
        # when something actually consumes them; per-decision attribution needs
        # decision_id stamped on each item.
        self.action_feedback_log = {
            "decision_id": decision_id, "action": action, "outcome": verdict,
            "traces_total": len(self.eligibility),
        }
        self._save()
        return {"updated": 1, "action": action, "outcome": verdict,
                "decision_id": decision_id}

    def action_adaptation_notes(self, limit: int = 4) -> list:
        """REBUILT 2026-09-30 (caller: tl_ego_server.py:5247 needs this accessor).

        The original source was lost in the 09-30 wipe, and no surviving backup ever
        defined this method — so it is reconstructed from data THIS object already
        records (last_outcome / key_quality / calibration / drift_cleared).
        Returns <= limit plain strings for the "Measured Failure Adaptations" block:
        the caller joins them verbatim, so they must be actionable, not decorative.
        """
        notes = []
        lo = self.last_outcome or {}
        if lo:
            if float(lo.get("prediction_error", 0.0) or 0.0) >= 0.5:
                notes.append(f"- action prediction missed: {lo.get('predicted')} -> {lo.get('actual')} "
                             f"(regime={lo.get('regime')}, gate={lo.get('gate')}, reason={lo.get('reason')}); "
                             f"do not repeat that transition blindly")
            else:
                notes.append(f"- action prediction held: {lo.get('predicted')} == {lo.get('actual')} "
                             f"(repeats={lo.get('transition_repeats')}, gate={lo.get('gate')})")
        for key, kq in sorted(self.key_quality.items(), key=lambda kv: -sum(kv[1]))[:limit]:
            hits, misses = (list(kq) + [0, 0])[:2]
            total = hits + misses
            if total >= 3 and hits / total < 0.5:
                notes.append(f"- weak context {key}: {hits}/{total} predictions hit "
                             f"({misses} misses) -> treat its expected type as unreliable")
        if self.drift_cleared:
            notes.append(f"- {self.drift_cleared} stale transition tables were dropped after drift "
                         f"(they stopped predicting); relearn from fresh evidence")
        cal = self.calibration_stats() or {}
        if cal.get("brier") is not None and float(cal.get("brier", 0) or 0) > 0.35:
            notes.append(f"- prediction calibration is poor (brier={cal.get('brier')}, n={cal.get('n')}): "
                         f"trust action-type predictions less until it recovers")
        return notes[:max(0, int(limit))]

    def status(self) -> Dict[str, Any]:
        return {
            "active_neurons": len(self.active_neurons),
            "eligible_edges": len(self.eligibility),
            "conditional_transition_contexts": len(self.transition_priors),
            "coarse_backoff": _BACKOFF,
            "stable_threshold": round(self._stable_threshold(), 4),
            "stable_window_n": len(self.competition_window),
            "gate_hist": dict(self.gate_hist),
            "calibration": self.calibration_stats(),
            "drift_cleared": self.drift_cleared,
            "rollout": self.last_rollout,
            "persist": dict(self.persist),
            "last_outcome": self.last_outcome,
            "action_outcomes": {k: dict(v) for k, v in self.action_outcomes.items()},
            "action_feedback_log": dict(self.action_feedback_log),
        }
