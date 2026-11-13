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
# 2026-09-06: was Path.home() — service runs as LocalSystem, whose home is
# systemprofile, so graph_db.exists() was always False → graph retrieval
# silently returned [] under nssm (worked only in user-shell tests).
GRAPH_DB = Path(__file__).resolve().parents[3] / "graph-memory.db"
# 2026-08-29 fix: was Path.home()/"memory"/"memu.db" (never existed → retrieval
# layer 3 was permanently dead). Now points at the live DB that tl_ego_server
# (llm_classify [Past Experience]) and sync_mc_activity write/read.
AGENT_MEMORY_DB = Path(__file__).resolve().parents[2] / "memory" / "memu.db"

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
        """从 agent-memory (memu.db) 关键词检索，替代已禁用的 Hy-Memory。

        2026-08-29 fix: 旧实现 (a) 指向不存在的 ~/memory/memu.db（第三层检索
        永远返回空），(b) 查询虚构的 memories 表 schema（实际表是 memory_items，
        列为 summary/content/memory_type/confidence/recalls/salience/ts），
        (c) 直接整串 LIKE，中文长句永远无法命中。现按活库真实 schema 重写，
        并移植 tl_ego_server._memory_search 的 CJK bigram 分词。
        """
        try:
            db_path = Path(AGENT_MEMORY_DB)
            if not db_path.exists():
                return []
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()

            # Tokenize like tl_ego_server._memory_search: CJK words + bigrams,
            # ASCII words as-is, so CJK queries can actually hit rows.
            token_set = set()
            for tok in query.strip().split():
                if len(tok) >= 2:
                    token_set.add(tok)
                    if any('\u4e00' <= ch <= '\u9fff' for ch in tok):
                        for i in range(len(tok) - 1):
                            token_set.add(tok[i:i + 2])
            tokens = [t for t in token_set if len(t) >= 2]
            if not tokens:
                conn.close()
                return []

            # 2026-09-06: same expression-tree cap as _retrieve_graph —
            # CJK bigrams blow past SQLite's 1000-depth OR limit.
            def _tok_rank(s):
                return (all(c.isascii() for c in s), len(s))
            tokens = sorted(tokens, key=_tok_rank, reverse=True)[:12]

            clauses = []
            params = []
            for tok in tokens:
                p = f"%{tok}%"
                clauses.append("(summary LIKE ? OR content LIKE ?)")
                params.extend([p, p])

            where_sql = " OR ".join(clauses)
            params.append(limit)
            # Rank like the server: confidence dominates, fresh rows win ties.
            cur.execute(
                f"SELECT id, summary, content, memory_type, confidence, recalls, ts "
                f"FROM memory_items WHERE ({where_sql}) "
                f"ORDER BY (salience * 0.4 + confidence * 10 + MIN(recalls, 50) * 0.1 "
                f"+ (ts - strftime('%s','now')) / 86400.0 * 24) DESC, id DESC "
                f"LIMIT ?",
                params
            )
            rows = cur.fetchall()
            conn.close()

            lines = []
            for row in rows:
                mem_id, summary, content, mem_type, confidence, recalls, ts = row
                text_preview = (content or summary or "")[:300]
                src = mem_type or "agent"
                try:
                    score = float(confidence or 0)
                except (TypeError, ValueError):
                    score = 0.0
                lines.append(f"  [{src}|score={score:.2f}|{mem_id}] {text_preview}")
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

            # 2026-09-06 fix: whitespace-only split turned a whole CJK sentence
            # into ONE token that LIKE-matched nothing (graph retrieval blind
            # for Chinese queries). Port the CJK bigram tokenizer from
            # tl_ego_server._memory_search so mixed CN/EN queries hit rows.
            import re as _re
            _CJK = _re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
            token_set = set()
            for tok in tokens:
                parts = _re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff\u3400-\u4dbf]+", tok)
                for part in parts:
                    if _CJK.match(part):
                        if 2 <= len(part) <= 12:
                            token_set.add(part)
                        for i in range(len(part) - 1):
                            token_set.add(part[i:i + 2])
                    elif len(part) >= 2:
                        token_set.add(part)
            tokens = [t for t in token_set if len(t) >= 2]
            if not tokens:
                conn.close()
                return []

            # 2026-09-06: cap clause count — CJK bigrams from one sentence
            # produce 100+ OR-terms -> SQLite "Expression tree is too large
            # (maximum depth 1000)" -> retrieval silently returns []. Keep
            # the most specific tokens: full ASCII keywords first, then
            # longest CJK words.
            def _tok_rank(s):
                return (all(c.isascii() for c in s), len(s))
            tokens = sorted(tokens, key=_tok_rank, reverse=True)[:12]

            clauses = []
            params = []
            for tok in tokens:
                p = f"%{tok}%"
                # 2026-09-06: include name — TASK/EVENT nodes carry the key
                # facts in name (e.g. 'world-verify-tick-7667',
                # 'torch-line-third-verification'), not content.
                clauses.append("(name LIKE ? OR content LIKE ? OR description LIKE ? OR type LIKE ?)")
                params.extend([p, p, p, p])

            where_sql = " OR ".join(clauses)
            params.append(limit)
            # 2026-09-06 fix: created_at has mixed types (35931 int-ms vs 1132
            # ISO-text rows). SQLite sorts text > integers, so bare
            # ORDER BY created_at DESC always surfaced stale ISO rows
            # (2026-07 arxiv backfill) above today's records. Normalize both
            # to epoch seconds for ordering. CAST is required: strftime
            # returns TEXT which sorts above any REAL.
            _TS = "CASE typeof(created_at)\n                             WHEN 'integer' THEN created_at / 1000.0\n                             ELSE CAST(strftime('%s', substr(created_at, 1, 19)) AS REAL)\n                           END"
            # Name matches first (TASK/EVENT names carry the facts), then
            # content matches — else query-echo events crowd out the real
            # verification records.
            cur.execute(
                f"SELECT type, name, description, content, {_TS} AS ts_s "
                f"FROM gm_nodes WHERE ({where_sql}) AND name LIKE ? "
                f"ORDER BY ts_s DESC LIMIT ?",
                params[:-1] + [f"%{tokens[0]}%", limit]
            )
            rows = cur.fetchall()
            if len(rows) < limit:
                seen = {r["name"] for r in rows}
                cur.execute(
                    f"SELECT type, name, description, content, {_TS} AS ts_s "
                    f"FROM gm_nodes WHERE ({where_sql}) "
                    f"ORDER BY ts_s DESC LIMIT ?",
                    params
                )
                for r in cur.fetchall():
                    if r["name"] not in seen:
                        rows.append(r)
                        seen.add(r["name"])
                        if len(rows) >= limit:
                            break
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

    def _write_memu(self, text: str) -> bool:
        """Idempotent insert into agent-memory (memu.db).

        The deterministic keyword channel: no LLM in the path, so recall's
        LIKE tokens hit it regardless of hy-memory's extractor mood.
        Idempotent on content so the hy fallback and the sediment dual-write
        cannot duplicate rows.
        """
        try:
            conn = sqlite3.connect(str(AGENT_MEMORY_DB))
            conn.execute(
                "INSERT INTO memory_items (summary, content, memory_type, confidence, recalls, salience, ts) "
                "SELECT ?, ?, 'contemplation', 0.9, 0, 0.5, strftime('%s','now') "
                "WHERE NOT EXISTS (SELECT 1 FROM memory_items WHERE content = ?)",
                (text[:200], text, text)
            )
            conn.commit(); conn.close()
            return True
        except Exception as e:
            print(f"  [memory_bridge] memu.db write failed: {e}")
            return False

    def _write_hy(self, text: str) -> bool:
        """写入 Hy-Memory"""
        # 2026-10-27: a 200 response with success:false (502 EMPTY_RESPONSE —
        # the extractor LLM returned empty on this content) used to be a
        # silent drop. Content-triggered, so retrying the same text never
        # helps; route it to the same memu.db fallback as the exception path.
        _ok = False
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
            _ok = bool(result.get("success", False))
            if not _ok:
                print(f"  [memory_bridge] Hy-Memory rejected: "
                      f"{str(result.get('error_message'))[:120]}", flush=True)
        except Exception as e:
            print(f"  [memory_bridge] Hy-Memory write failed: {e}")
        if _ok:
            return True
        # 2026-09-06: fallback to agent-memory (memu.db) so remember()
        # still persists when Hy-Memory (:19527) is down — otherwise
        # writes silently fail and recall has nothing to find.
        if self._write_memu(text):
            print("  [memory_bridge] Hy-Memory unavailable → wrote to memu.db fallback", flush=True)
            return True
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