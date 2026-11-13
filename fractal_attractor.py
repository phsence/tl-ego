"""
P5 分形递归 Attractor — FractalRecursiveAttractor
====================================================
层级结构：
  Level 3  190K 神经元（当前 TL-Ego，含 KWTA + STDP + EdgeStore）
  Level 2  Type 微 Attractor（19 个 Type，每个是神经元的子集）
  Level 1  Pattern Cluster（Type 竞争向量的时间模式聚类）

设计目标：
  1. 在 KWTA 结果之上叠加一层"模式识别"
  2. 识别跨时间维度的 Type 竞争模式（如 "sensory主导 → memory巩固 → decider决策"）
  3. 每层只向下游传递压缩摘要，不做全量计算

集成点：
  - tl_ego_server.py init()     → FractalAttractorManager()
  - resting_tick()              → on_kwta_complete(tick, type_vector, dominant, concentration)
  - /status                     → expose fractal_attractor section
"""

import threading
import math
import copy
from collections import deque, defaultdict
from typing import Dict, List, Optional

# ─── 配置 ────────────────────────────────────────────────────────────
TYPE_NAMES = [
    "sensory", "integrator", "decider", "action", "memory",
    "rule_modification", "veto", "doubt", "curiosity", "planning",
    "social", "ethical", "aesthetic", "temporal", "emotional",
    "spatial", "cultural", "meta", "architect",
]

# Level 2 参数
LA2_HISTORY_MAX = 200        # 每个 Type 保留的激活历史长度
LA2_ACTIVE_THRESHOLD = 0.05  # concentration > 此值视为"激活"

# Level 1 参数
LA1_WINDOW = 8               # 用最近 N 个 tick 的 type_vector 做模式检测
LA1_MIN_CLUSTER_SIZE = 2     # 最小模式簇成员数
LA1_CLUSTER_MAX = 12         # 支持终局验收要求的至少 10 个自发模式簇
LA1_HISTORY_MAX = 500        # type_vector 历史最多保留

# 统计
_stats_lock = threading.Lock()
_fa_stats = {
    "enabled": False,
    "level2_sub_attractors": 0,
    "level1_pattern_clusters": 0,
    "total_type_vectors": 0,
    "last_update_tick": 0,
    "last_pattern_detect_tick": 0,
    "current_pattern_id": None,   # 当前激活的模式簇 ID
    "dominant_level2": "",        # 当前最活跃的 Type（Level 2 winner）
    "dominant_level1": "",        # 当前最活跃的模式簇名（Level 1 winner）
}


# ─── Level 2 — Type 微 Attractor ────────────────────────────────────

class SubAttractor:
    """
    Level 2: 每个 Type 作为一个微 Attractor。
    追踪该 Type 的激活浓度历史、最近主导次数、平均浓度。
    """

    __slots__ = ("type_name", "history", "dom_history", "dom_count", "total_conc", "last_tick")

    def __init__(self, type_name: str):
        self.type_name = type_name
        self.history: deque = deque(maxlen=LA2_HISTORY_MAX)   # [(tick, concentration)]
        self.dom_history: deque = deque(maxlen=LA2_HISTORY_MAX)
        self.dom_count: int = 0                                # 最近 N 次中成为 dominant 的次数
        self.total_conc: float = 0.0                           # 累积浓度（用于平均）
        self.last_tick: int = -1

    def update(self, tick: int, concentration: float, was_dominant: bool = False) -> None:
        if len(self.history) == self.history.maxlen:
            self.total_conc -= self.history[0][1]
        if len(self.dom_history) == self.dom_history.maxlen:
            self.dom_count -= int(self.dom_history[0])
        self.history.append((tick, concentration))
        self.dom_history.append(was_dominant)
        self.total_conc += concentration
        if was_dominant:
            self.dom_count += 1
        self.last_tick = tick

    @property
    def avg_conc(self) -> float:
        if not self.history:
            return 0.0
        return self.total_conc / len(self.history)

    @property
    def recent_dom_rate(self) -> float:
        """最近 LA2_HISTORY_MAX 次中的主导频率"""
        total = len(self.history)
        if total == 0:
            return 0.0
        return self.dom_count / total

    @property
    def is_active(self) -> bool:
        if not self.history:
            return False
        return self.history[-1][1] > LA2_ACTIVE_THRESHOLD

    def to_dict(self) -> dict:
        return {
            "type": self.type_name,
            "avg_conc": round(self.avg_conc, 4),
            "dom_rate": round(self.recent_dom_rate, 4),
            "is_active": self.is_active,
            "history_len": len(self.history),
            "last_tick": self.last_tick,
        }


# ─── Level 1 — Pattern Cluster（Type 竞争模式聚类）────────────────────

class PatternCluster:
    """
    Level 1: 一个"模式簇"——一组相似的 Type 竞争向量序列。
    例如：{sensory 高, memory 中, decider 低} 是一个模式；
          {integrator 高, curiosity 中} 是另一个。
    """

    __slots__ = (
        "cluster_id", "label", "member_ticks", "type_centroid",
        "dom_type", "feedback_type", "total_hits", "last_hit_tick",
    )

    def __init__(
        self,
        cluster_id: int,
        centroid: List[float],
        dom_type: str,
        feedback_type: Optional[str] = None,
    ):
        self.cluster_id = cluster_id
        self.label = f"pattern_{dom_type}"   # 以主导 Type 命名
        self.member_ticks: List[int] = []    # 属于此模式的 tick 列表
        self.type_centroid = centroid        # type_vector 的质心
        self.dom_type = dom_type
        self.feedback_type = feedback_type or dom_type
        self.total_hits: int = 0
        self.last_hit_tick: int = -1

    def add_hit(self, tick: int):
        self.member_ticks.append(tick)
        self.total_hits += 1
        self.last_hit_tick = tick

    def recency_score(self, current_tick: int) -> float:
        """越近的 hit 权重越高（指数衰减）"""
        if not self.member_ticks:
            return 0.0
        recent = [t for t in self.member_ticks if current_tick - t <= LA1_WINDOW * 3]
        if not recent:
            return 0.0
        # 越近权重越高
        total = sum(math.exp(-(current_tick - t) / LA1_WINDOW) for t in recent)
        return total / len(recent)

    def to_dict(self) -> dict:
        return {
            "id": self.cluster_id,
            "label": self.label,
            "dom_type": self.dom_type,
            "feedback_type": self.feedback_type,
            "hits": self.total_hits,
            "recent_hits": len([t for t in self.member_ticks
                                if not self.member_ticks or
                                t > self.member_ticks[-1] - LA1_WINDOW * 3]),
            "recency": round(self.recency_score(self.member_ticks[-1] if self.member_ticks else 0), 4),
        }


# ─── FractalAttractorManager（Level 1 控制器）─────────────────────────

class FractalAttractorManager:
    """
    P5 分形递归 Attractor 控制器。

    层级：
      Level 3  ←  tl_ego_server 中的 KWTA（190K 神经元竞争）
      Level 2  ←  self._type_attrs（19 个 Type 微 Attractor）
      Level 1  ←  self._pattern_clusters（Type 竞争模式聚类）

    每层只向下传递压缩摘要，不做全量计算。
    """

    def __init__(self, type_names: Optional[List[str]] = None):
        self._lock = threading.Lock()
        self._types = type_names or TYPE_NAMES
        self._enabled = False

        # Level 2: 19 个 Type 微 Attractor
        self._type_attrs: Dict[str, SubAttractor] = {
            t: SubAttractor(t) for t in self._types
        }

        # Level 1: type_vector 历史 + 模式簇
        self._type_vector_history: deque = deque(maxlen=LA1_HISTORY_MAX)
        # 格式: [(tick, type_vector: List[float], dominant_type: str, concentration: float)]

        self._pattern_clusters: Dict[int, PatternCluster] = {}
        self._next_cluster_id: int = 0
        self._current_pattern_id: Optional[int] = None
        self._vectors_seen: int = 0
        self._pending_feedback: Optional[dict] = None
        self._feedback_emitted: int = 0
        self._feedback_consumed: int = 0
        self._feedback_applied: int = 0
        self._last_applied_feedback: Optional[dict] = None

        # 当前快照（供 /status 读取）
        self._snapshot: dict = {"enabled": False}

        self._enabled = True

    # ── 外部接口 ─────────────────────────────────────────────────────

    def on_kwta_complete(
        self,
        tick: int,
        type_vector: List[float],
        dominant_type: str,
        concentration: float,
    ) -> dict:
        """
        每次 KWWA 竞争完成后调用。
        返回 Level 1 模式检测结果（供上层参考）。
        """
        if not self._enabled:
            return {}

        with self._lock:
            # ── Level 2 更新 ──
            for i, tname in enumerate(self._types):
                conc = type_vector[i] if i < len(type_vector) else 0.0
                was_dom = (tname == dominant_type)
                self._type_attrs[tname].update(tick, conc, was_dom)

            # 记录 type_vector 历史
            self._type_vector_history.append({
                "tick": tick,
                "vec": list(type_vector),
                "dom": dominant_type,
                "conc": concentration,
            })
            self._vectors_seen += 1

            # ── Level 1 模式检测（每 LA1_WINDOW 个向量跑一次） ──
            pattern_id = None
            if self._vectors_seen % LA1_WINDOW == 0:
                pattern_id = self._detect_patterns(tick)

            # 更新快照
            self._update_snapshot(tick, dominant_type, concentration, pattern_id)

            return self._snapshot.get("level1", {})

    # ── Level 1 模式检测 ─────────────────────────────────────────────

    def _detect_patterns(self, current_tick: int) -> Optional[int]:
        """
        用最近 LA1_WINDOW 个 type_vector 的**主方差方向**做模式检测。
        核心思想：同一组 Type 反复同步升降 → 是一个模式簇。
        """
        recent = list(self._type_vector_history)[-LA1_WINDOW:]
        if len(recent) < 2:
            return None

        # 计算每个 Type 在该窗口内的均值向量
        n_types = len(self._types)
        window_mean = [0.0] * n_types
        for entry in recent:
            vec = entry["vec"]
            for i in range(min(len(vec), n_types)):
                window_mean[i] += vec[i]
        window_mean = [v / len(recent) for v in window_mean]

        # l5g-20261031-062977: the docstring above specifies 主方差方向
        # (principal variance direction) but the mean was used. Measured on
        # real snapshots (ticks 62904-62990, 10 detections): the window-mean
        # direction is stationary in steady state (cos >= 0.9912 vs the lone
        # centroid every detection), so the birth test can never fail and
        # n_patterns stays 1 forever. Principal variance direction births 4
        # patterns on the identical data (replay_birth.py in the sandbox).
        # Power iteration on the centered window; a degenerate (zero-variance)
        # window falls back to the mean. Threshold and centroid-chase were
        # measured inert across their whole range - representation is the lever.
        centered = [[vec[i] - window_mean[i] for i in range(n_types)]
                    for vec in (e["vec"] for e in recent)]
        direction = list(centered[0]) if centered else list(window_mean)
        for _ in range(50):
            nxt = [0.0] * n_types
            for c in centered:
                d = sum(c[i] * direction[i] for i in range(n_types))
                for i in range(n_types):
                    nxt[i] += d * c[i]
            ln = math.sqrt(sum(x * x for x in nxt))
            if ln < 1e-12:
                break
            direction = [x / ln for x in nxt]
        if any(direction):
            j = max(range(n_types), key=lambda k: abs(direction[k]))
            if direction[j] < 0:
                direction = [-x for x in direction]
            summary = direction
        else:
            summary = window_mean

        # 把摘要向量归一化后和已有簇做相似度匹配
        norm = math.sqrt(sum(v * v for v in summary)) or 1.0
        normalized = [v / norm for v in summary]

        best_cluster_id = None
        best_similarity = -1.0

        for cid, cluster in self._pattern_clusters.items():
            sim = self._cosine_sim(normalized, cluster.type_centroid)
            if sim > best_similarity:
                best_similarity = sim
                best_cluster_id = cid

        # 2026-08-25: 0.75 -> 0.65. With heartbeat-cadence ticks (~20s/tick) the
        # window mean vectors drift between detections; at 0.75 nearly every
        # detection created a NEW cluster (clusters never reached
        # LA1_MIN_CLUSTER_SIZE=2 hits) so _publish_feedback never fired and
        # fractal feedback stayed emitted=0 forever. 0.65 lets recurring
        # patterns merge into existing clusters and actually accumulate hits.
        SIMILARITY_THRESHOLD = 0.65

        if best_similarity >= SIMILARITY_THRESHOLD and best_cluster_id is not None:
            # 归入已有簇
            cluster = self._pattern_clusters[best_cluster_id]
            cluster.add_hit(current_tick)
            # 更新质心（滑动平均）
            alpha = 0.1
            for i in range(len(cluster.type_centroid)):
                cluster.type_centroid[i] = (
                    (1 - alpha) * cluster.type_centroid[i] + alpha * normalized[i]
                )
            self._current_pattern_id = best_cluster_id
            self._publish_feedback(cluster, current_tick)
            return best_cluster_id

        elif len(self._pattern_clusters) < LA1_CLUSTER_MAX:
            # 新建模式簇
            # Label the cluster with the most frequent observed transition in
            # this window. Fall back to the strongest mean type when stable.
            dom_sequence = [entry["dom"] for entry in recent if entry["dom"] in self._types]
            transitions = [
                (source, target)
                for source, target in zip(dom_sequence, dom_sequence[1:])
                if source != target
            ]
            sorted_types = sorted(zip(window_mean, self._types), reverse=True)
            dom_type = sorted_types[0][1] if sorted_types else "unknown"
            if transitions:
                transition_counts = defaultdict(int)
                first_seen = {}
                for index, transition in enumerate(transitions):
                    transition_counts[transition] += 1
                source, target = max(
                    transition_counts,
                    key=lambda transition: (
                        transition_counts[transition],
                        -first_seen.get(transition, 0),  # 2026-08-13: first_seen never populated -> KeyError tuple
                    ),
                )
                label = f"pattern_{source}→{target}"
                # Route feedback to the observed transition target so seed_adjustments
                # reinforce whichever type the data shows is transitioning, not the
                # persistent dominant type. A special-case for integrator would starve
                # rule_modification of seed reinforcement and create structural suppression.
                feedback_type = target
            else:
                label = f"pattern_{dom_type}"
                feedback_type = dom_type
            new_cluster = PatternCluster(
                cluster_id=self._next_cluster_id,
                centroid=list(normalized),
                dom_type=dom_type,
                feedback_type=feedback_type,
            )
            new_cluster.label = label  # 覆盖默认 label
            new_cluster.add_hit(current_tick)
            self._pattern_clusters[self._next_cluster_id] = new_cluster
            self._next_cluster_id += 1
            self._current_pattern_id = new_cluster.cluster_id
            return new_cluster.cluster_id

        # 簇已满且不匹配任何现有簇 → 归入最相似的
        if best_cluster_id is not None:
            cluster = self._pattern_clusters[best_cluster_id]
            cluster.add_hit(current_tick)
            self._current_pattern_id = best_cluster_id
            self._publish_feedback(cluster, current_tick)
            return best_cluster_id

        return None

    def _publish_feedback(self, cluster: PatternCluster, tick: int) -> None:
        if cluster.total_hits < LA1_MIN_CLUSTER_SIZE:
            return
        self._pending_feedback = {
            "pattern_id": cluster.cluster_id,
            "pattern_label": cluster.label,
            "target_type": cluster.feedback_type,
            "tick": tick,
        }
        self._feedback_emitted += 1

    @staticmethod
    def _cosine_sim(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
        norm_b = math.sqrt(sum(y * y for y in b)) or 1.0
        return dot / (norm_a * norm_b)

    # ── 快照更新 ─────────────────────────────────────────────────────

    def _update_snapshot(self, tick, dominant_type, concentration, pattern_id):
        l2_summary = {}
        active_types = []
        for tname, attr in self._type_attrs.items():
            d = attr.to_dict()
            l2_summary[tname] = d
            if d["is_active"]:
                active_types.append(tname)

        # Level 1 最活跃的模式
        best_pid = None
        best_score = -1.0
        for cid, cluster in self._pattern_clusters.items():
            score = cluster.recency_score(tick)
            if score > best_score:
                best_score = score
                best_pid = cid

        self._snapshot = {
            "enabled": True,
            "tick": tick,
            "level2": {
                "dominant": dominant_type,
                "concentration": round(concentration, 4),
                "active_types": active_types,
                "type_summary": l2_summary,
            },
            "level1": {
                "current_pattern_id": pattern_id,
                "dominant_pattern_id": best_pid,
                "n_patterns": len(self._pattern_clusters),
                "patterns": {cid: c.to_dict() for cid, c in self._pattern_clusters.items()},
            },
            "feedback": {
                "pending": dict(self._pending_feedback) if self._pending_feedback else None,
                "emitted": self._feedback_emitted,
                "consumed": self._feedback_consumed,
                "seed_adjustments_total": self._feedback_applied,
                "last_applied": dict(self._last_applied_feedback) if self._last_applied_feedback else None,
            },
        }

    # ── 状态读取 ─────────────────────────────────────────────────────

    def get_status(self) -> dict:
        with self._lock:
            status = copy.deepcopy(self._snapshot)
            feedback = status.setdefault("feedback", {})
            feedback.update({
                "pending": dict(self._pending_feedback) if self._pending_feedback else None,
                "emitted": self._feedback_emitted,
                "consumed": self._feedback_consumed,
                "seed_adjustments_total": self._feedback_applied,
                "last_applied": dict(self._last_applied_feedback) if self._last_applied_feedback else None,
            })
            return status

    def get_type_summary(self) -> dict:
        with self._lock:
            return {t: attr.to_dict() for t, attr in self._type_attrs.items()}

    def get_pattern_clusters(self) -> dict:
        with self._lock:
            return {cid: c.to_dict() for cid, c in self._pattern_clusters.items()}

    def consume_seed_feedback(self) -> Optional[dict]:
        with self._lock:
            if self._pending_feedback is None:
                return None
            feedback = dict(self._pending_feedback)
            self._pending_feedback = None
            self._feedback_consumed += 1
            return feedback

    def mark_seed_feedback_applied(self, feedback: dict) -> None:
        with self._lock:
            self._feedback_applied += 1
            self._last_applied_feedback = dict(feedback)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def level1_dominant(self) -> str:
        with self._lock:
            best_pid = self._snapshot.get("level1", {}).get("dominant_pattern_id")
            if best_pid is not None and best_pid in self._pattern_clusters:
                return self._pattern_clusters[best_pid].label
            return ""

    @property
    def level2_dominant(self) -> str:
        with self._lock:
            return self._snapshot.get("level2", {}).get("dominant", "")

    @property
    def active_types(self) -> List[str]:
        with self._lock:
            return self._snapshot.get("level2", {}).get("active_types", [])


# ─── 全局单例访问 ────────────────────────────────────────────────────

_manager: Optional[FractalAttractorManager] = None
_manager_lock = threading.Lock()


def get_fractal_manager() -> Optional[FractalAttractorManager]:
    with _manager_lock:
        return _manager


def init_fractal(type_names: Optional[List[str]] = None) -> FractalAttractorManager:
    global _manager
    with _manager_lock:
        _manager = FractalAttractorManager(type_names)
        return _manager


def get_stats() -> dict:
    with _stats_lock:
        return dict(_fa_stats)


def update_stats(**kwargs):
    with _stats_lock:
        _fa_stats.update(kwargs)
