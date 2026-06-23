#!/usr/bin/env python3
"""
memory_bridge.py — TL-Ego 长期记忆桥接层

连接 TL-Ego 神经引擎与外部记忆系统：
  - Hy-Memory (HTTP API :19527) — 语义向量记忆，长期存储
  - graph-memory (SQLite) — 知识图谱，关系型记忆

职责：
  1. retrieve(query) → 从双存储检索相关记忆，返回格式化上下文
  2. write(query, result, ego_state) → 写入双存储
  3. write_music_event(query, features, ego_reaction) → 音乐事件专用

用法：
  from memory_bridge import MemoryBridge
  bridge = MemoryBridge()
  context = bridge.retrieve("what is my current mental state?")
  bridge.write("what is my current state?", result, ego_state)
"""

import json
import time
import sqlite3
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Dict, Any, List

# ─── 路径 ─────────────────────────────────────────────
WORKSPACE = Path(__file__).resolve().parent.parent.parent
GRAPH_DB = Path.home() / ".openclaw" / "graph-memory.db"
AGENT_MEMORY_DB = Path.home() / "memory" / "memu.db"

# ─── Hy-Memory 端点 ───────────────────────────────────
HY_MEMORY_URL = "http://127.0.0.1:19527"
HY_MEMORY_USER = "tl-ego"
HY_MEMORY_AGENT = "core"


class MemoryBridge:
    """TL-Ego 记忆桥接层。无状态，每次调用独立。"""

    def __init__(self, hy_url: str = HY_MEMORY_URL, user_id: str = HY_MEMORY_USER,
                 agent_id: str = HY_MEMORY_AGENT, graph_db: Path = GRAPH_DB):
        self.hy_url = hy_url
        self.user_id = user_id
        self.agent_id = agent_id
        self.graph_db = graph_db
        self.agent_memory_db = Path(AGENT_MEMORY_DB)

    # ═══════════════════════════════════════════════════════════════
    # 检索 — 从三存储拿记忆
    # ═══════════════════════════════════════════════════════════════

    def retrieve(self, query: str, limit: int = 5) -> str:
        """
        从 Hy-Memory 和 graph-memory 检索相关记忆。
        返回格式化的上下文字符串，可直接注入 S2 prompt。
        失败时返回空字符串，不抛异常。
        """
        parts = []

        # 1. Hy-Memory 语义检索
        hy_results = self._retrieve_hy(query, limit)
        if hy_results:
            parts.append("[Long-Term Memory (Hy-Memory)]")
            parts.extend(hy_results)

        # 2. graph-memory 关键词检索
        gm_results = self._retrieve_graph(query, limit)
        if gm_results:
            parts.append("[Knowledge Graph Memory]")
            parts.extend(gm_results)

        # 3. agent-memory (memu.db) 关键词检索
        am_results = self._retrieve_agent_memory(query, limit)
        if am_results:
            parts.append("[Agent Memory]")
            parts.extend(am_results)

        # 4. Fallback: read recent [Important] entries from daily memory files
        if not parts:
            fallback = self._retrieve_memory_files(query, limit)
            if fallback:
                parts.append("[Recent Memory Files]")
                parts.extend(fallback)

        if not parts:
            return ""

        return "\n".join(parts) + "\n"

    def _retrieve_hy(self, query: str, limit: int) -> List[str]:
        """调用 Hy-Memory /api/v1/search"""
        try:
            payload = {
                "query": query,
                "user_ids": [self.user_id],
                "limit": limit,
                "min_score": 0.4,
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.hy_url}/api/v1/search",
                data=data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())

            lines = []
            memories = result.get("memories", {})
            for channel in ("profile", "normal", "proactive"):
                for mem in memories.get(channel, []):
                    content = mem.get("content", "")[:300]
                    score = mem.get("score", 0)
                    layer = mem.get("layer", "?")
                    mem_id = mem.get("memory_id", "")[:8]
                    if content.strip():
                        lines.append(f"  [{layer}|score={score:.2f}|{mem_id}] {content}")
            return lines
        except Exception as e:
            print(f"  [memory_bridge] Hy-Memory retrieve failed: {e}")
            return []

    def _retrieve_agent_memory(self, query: str, limit: int) -> List[str]:
        """从 agent-memory (memu.db) 关键词检索，替代已禁用的 Hy-Memory。"""
        try:
            db_path = Path.home() / "memory" / "memu.db"
            if not db_path.exists():
                return []
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()

            tokens = [t for t in query.strip().split() if len(t) >= 2]
            if not tokens:
                conn.close()
                return []

            clauses = []
            params = []
            for tok in tokens:
                p = f"%{tok}%"
                clauses.append("(text LIKE ? OR summary LIKE ?)")
                params.extend([p, p])

            where_sql = " OR ".join(clauses)
            params.append(limit)
            cur.execute(
                f"SELECT id, text, type, created_at, score "
                f"FROM memories WHERE ({where_sql}) "
                f"ORDER BY created_at DESC LIMIT ?",
                params
            )
            rows = cur.fetchall()
            conn.close()

            lines = []
            for row in rows:
                mem_id, text, mem_type, created_at, score = row
                text_preview = (text or "")[:300]
                src = mem_type or "agent"
                lines.append(f"  [{src}|score={score or 0:.2f}|{mem_id}] {text_preview}")
            return lines
        except Exception as e:
            print(f"  [memory_bridge] agent-memory retrieve failed: {e}")
            return []

    def _retrieve_graph(self, query: str, limit: int) -> List[str]:
        """直接查 graph-memory.db 的 nodes 表"""
        try:
            if not self.graph_db.exists():
                return []
            conn = sqlite3.connect(str(self.graph_db))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            tokens = [t for t in query.strip().split() if len(t) >= 2]
            if not tokens:
                conn.close()
                return []

            clauses = []
            params = []
            for tok in tokens:
                p = f"%{tok}%"
                clauses.append("(content LIKE ? OR description LIKE ? OR type LIKE ?)")
                params.extend([p, p, p])

            where_sql = " OR ".join(clauses)
            params.append(limit)
            cur.execute(
                f"SELECT type, name, description, content, created_at "
                f"FROM gm_nodes WHERE ({where_sql}) "
                f"ORDER BY created_at DESC LIMIT ?",
                params
            )
            rows = cur.fetchall()
            conn.close()

            lines = []
            for r in rows:
                desc = r["description"] or r["content"] or ""
                lines.append(f"  [{r['type']}|graph-memory] {desc[:200]}")
            return lines
        except Exception as e:
            print(f"  [memory_bridge] graph-memory retrieve failed: {e}")
            import traceback; traceback.print_exc()
            return []

    def _retrieve_memory_files(self, query: str, limit: int) -> List[str]:
        """
        Fallback: scan memory/ directory for recent [Important] entries
        and MEMORY.md milestones. Returns formatted lines.
        """
        try:
            memory_dir = WORKSPACE / "memory"
            lines = []

            # 1. Scan recent daily memory files (last 3 days)
            if memory_dir.exists():
                md_files = sorted(memory_dir.glob("2*.md"), reverse=True)[:5]
                for mf in md_files:
                    date_tag = mf.stem  # e.g. 2026-06-07
                    try:
                        text = mf.read_text(encoding="utf-8", errors="replace")
                        # Extract [Important] lines
                        for line in text.split("\n"):
                            stripped = line.strip()
                            if "[Important]" in stripped or "[Critical]" in stripped:
                                # Clean up markdown formatting
                                cleaned = re.sub(r"[*#_`\[\]]", "", stripped)[:250]
                                if len(cleaned) > 10:
                                    lines.append(f"  [{date_tag}] {cleaned}")
                    except Exception:
                        continue

            # 2. Scan MEMORY.md milestones
            memory_md = WORKSPACE / "MEMORY.md"
            if memory_md.exists():
                try:
                    text = memory_md.read_text(encoding="utf-8", errors="replace")
                    # Extract milestone entries (lines starting with - 2026- or marked Important)
                    for line in text.split("\n"):
                        stripped = line.strip()
                        if stripped.startswith("- 2026-") or "[Important]" in stripped:
                            cleaned = re.sub(r"[*#_`\[\]]", "", stripped)[:200]
                            if len(cleaned) > 10:
                                lines.append(f"  [milestone] {cleaned}")
                except Exception:
                    pass

            # 3. Rank by recency relevance to query (simple keyword overlap)
            query_tokens = set(t.lower() for t in query.strip().split() if len(t) >= 2)
            if query_tokens:
                scored = []
                for line in lines:
                    line_tokens = set(t.lower() for t in line.split() if len(t) >= 2)
                    overlap = len(query_tokens & line_tokens)
                    scored.append((overlap, line))
                scored.sort(key=lambda x: (-x[0], x[1]))
                lines = [s[1] for s in scored[:limit]]
            else:
                lines = lines[:limit]

            return lines
        except Exception as e:
            print(f"  [memory_bridge] memory files retrieve failed: {e}")
            return []

    # ═══════════════════════════════════════════════════════════════
    # 写入 — 双写 Hy-Memory + graph-memory
    # ═══════════════════════════════════════════════════════════════

    def write(self, query: str, result: dict, ego_state: dict = None) -> bool:
        """
        将一次 contemplation 的结果写入记忆系统。

        写入内容：
          - Hy-Memory: 语义文本（可被向量检索）
          - graph-memory: 结构化节点（可被图谱查询）

        不抛异常，失败时返回 False。
        """
        ego_state = ego_state or {}
        agg = result.get("aggregator", {})
        s1 = result.get("system1", {})

        # 构建记忆文本
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        content = (
            f"[{timestamp}] Ego Contemplation\n"
            f"Query: {query[:200]}\n"
            f"S1 type: {s1.get('dominant_type','?')} (conc={s1.get('concentration',0):.3f})\n"
            f"S2 type: {agg.get('s2_type','?')} (conf={agg.get('s2_confidence',0):.3f})\n"
            f"Verdict: {agg.get('verdict','?')} (agreement={agg.get('agreement',False)})\n"
            f"Energy: {ego_state.get('energy_level','?')} "
            f"Mood: {ego_state.get('mood','?')} "
            f"Focus: {ego_state.get('focus_areas','?')}"
        )

        # 写入 Hy-Memory
        hy_ok = self._write_hy(content)

        # 写入 graph-memory（通过 gm_record 工具的标准格式）
        gm_ok = self._write_graph(query, result)

        return hy_ok or gm_ok  # 至少一个成功就算成功

    def _write_hy(self, text: str) -> bool:
        """写入 Hy-Memory"""
        try:
            payload = {
                "text": text,
                "user_id": self.user_id,
                "agent_id": self.agent_id,
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.hy_url}/api/v1/add",
                data=data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
            return result.get("success", False)
        except Exception as e:
            print(f"  [memory_bridge] Hy-Memory write failed: {e}")
            return False

    def _write_graph(self, query: str, result: dict) -> bool:
        """
        写入 graph-memory SQLite (nodes 表).
        schema: id, type, name, description, content, created_at
        """
        try:
            if not self.graph_db.exists():
                return False

            agg = result.get("aggregator", {})
            s1 = result.get("system1", {})

            _q_slug = query[:40].replace(' ', '-').replace('/', '-') or 'contemplation'
            _s2_type = agg.get('s2_type', '?')
            _verdict = agg.get('verdict', '?')
            name = f"ego-{_s2_type}-{_verdict}"
            etype = "EVENT"
            summary = (
                f"TL-Ego [{_s2_type}/{_verdict}] {query[:80]}"
            )
            content = (
                f"Query: {query[:300]}\n"
                f"S1: {s1.get('dominant_type','?')} conc={s1.get('concentration',0):.3f} comp={s1.get('competition',0):.3f}\n"
                f"S2: {_s2_type} conf={agg.get('s2_confidence',0):.3f} sub={agg.get('s2_sub_type','')}\n"
                f"Verdict: {_verdict} agreement={agg.get('agreement',False)} margin={agg.get('winner_margin',0):.3f}"
            )

            conn = sqlite3.connect(str(self.graph_db))
            cur = conn.cursor()
            now_ms = int(time.time() * 1000)
            cur.execute(
                """INSERT INTO gm_nodes (type, name, description, content, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (etype, name, summary, content, now_ms)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"  [memory_bridge] graph-memory write failed: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # 音乐事件专用
    # ═══════════════════════════════════════════════════════════════

    def write_music_event(self, prompt: str, features: dict,
                          ego_reaction: dict = None) -> bool:
        """
        写入一次音乐感知事件。
        
        Args:
            prompt: ACE 生成 prompt
            features: 音频分析特征 (7层感官或6维)
            ego_reaction: TL-Ego contemplation 结果
        """
        ego_reaction = ego_reaction or {}
        agg = ego_reaction.get("aggregator", {})
        s1 = ego_reaction.get("system1", {})

        # 提取关键特征
        if features.get("_analyzer") == "sensory":
            tx = features.get("texture", {})
            h = features.get("harmonic", {})
            r = features.get("rhythm", {})
            d = features.get("dynamics", {})
            feature_text = (
                f"key={h.get('key','?')} bpm={r.get('bpm','?')} "
                f"valence={tx.get('emotional_valence',0):.2f} "
                f"arousal={tx.get('arousal',0):.2f} "
                f"density={tx.get('density','?')} "
                f"complexity={tx.get('complexity',0):.3f} "
                f"dynamic_range={d.get('dynamic_range_db',0)}dB "
                f"brightness={tx.get('brightness_character','?')}"
            )
        else:
            feature_text = (
                f"bpm={features.get('bpm','?')} "
                f"key={features.get('key','?')} "
                f"grade={features.get('grade','?')}"
            )

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        content = (
            f"[{timestamp}] Music Sensory Event\n"
            f"Prompt: {prompt[:120]}\n"
            f"Features: {feature_text}\n"
            f"Ego S1: {s1.get('dominant_type','?')} (comp={s1.get('competition',0):.3f})\n"
            f"Ego S2: {agg.get('s2_type','?')} (conf={agg.get('s2_confidence',0):.3f})\n"
            f"Ego Verdict: {agg.get('verdict','?')} agreement={agg.get('agreement',False)}"
        )

        # 写入 Hy-Memory
        hy_ok = self._write_hy(content)

        # 同时写入 graph-memory
        try:
            if not self.graph_db.exists():
                return hy_ok

            name = f"music-sensory-{int(time.time())}"
            description = f"Music-Ego sensory event: {prompt[:60]}"
            ego_summary = (
                f"S1={agg.get('s2_type',s1.get('dominant_type','?'))} "
                f"verdict={agg.get('verdict','?')} "
                f"agreement={agg.get('agreement',False)}"
            )

            conn = sqlite3.connect(str(self.graph_db))
            cur = conn.cursor()
            now_ms = int(time.time() * 1000)
            cur.execute(
                """INSERT INTO gm_nodes 
                   (type, name, description, content, status, importance, confidence_score, 
                    created_at, updated_at, category)
                   VALUES (?, ?, ?, ?, 'active', 0.6, 0.7, ?, ?, 'music')""",
                ("EVENT", name, description, content, now_ms, now_ms)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"  [memory_bridge] graph-memory music write failed: {e}")
            return hy_ok  # Hy-Memory 成功也算成功


# ─── 全局单例（可选） ───────────────────────────────────
_bridge_instance: Optional[MemoryBridge] = None


def get_bridge() -> MemoryBridge:
    """获取 MemoryBridge 全局单例"""
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = MemoryBridge()
    return _bridge_instance


# ─── 快速测试 ──────────────────────────────────────────
if __name__ == "__main__":
    bridge = MemoryBridge()

    # 测试检索
    print("=== 检索测试 ===")
    ctx = bridge.retrieve("music ego state")
    print(ctx or "(no results)")

    # 测试写入
    print("\n=== 写入测试 ===")
    ok = bridge._write_hy("TL-Ego bridge test: memory integration working")
    print(f"Hy-Memory write: {ok}")

    print("\n记忆桥接层就绪。")