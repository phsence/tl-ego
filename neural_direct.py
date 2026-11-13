"""NeuralDirect gate (2026-09-01, option 3: neural substrate decides directly).

Zero-LLM direct answering for queries whose answer is already grounded in a
VERIFIED CORRECTION row (memu.db memory_items) and whose semantic match is
confirmed by the same BGE embedding service (port 8002) the stage-1 pipeline
uses. This is the smallest real instance of "experience rewrites behavior":
corrected facts are answered by the substrate, without renting the frozen
stepfun brain to re-reason from scratch.

Gate (calibrated by offline tests on real data, 2026-09-01):
  BGE-M3 cosine baselines: same-topic paraphrase ~0.90, same-shape
  different-topic ~0.67, unrelated ~0.62. Therefore:
    fired = correction_row_top1 AND cos >= 0.80 AND token_overlap >= 2
            AND NOT num_guard
  num_guard: query mentions numbers absent from the row -> stored answer was
  bound to different conditions (50m vs 500m trap) -> refuse.

Fail-safe: ANY exception -> {'fired': False} -> caller uses normal LLM path.
Disable: env EGO_NEURAL_DIRECT=0.
"""
import hashlib
import json  # jev-cal 2026-09-20: calibration JSONL log
import os
import re  # jev-cal 2026-09-20: _parse_judge_probs (module-level: helper fn needs it)
import time

_DISABLED = os.environ.get("EGO_NEURAL_DIRECT", "1") == "0"
_MARKERS = (
    "\u4f60\u521a\u624d\u7b54\u9519\u4e86",        # ni gang da cuo le
    "\u7b54\u9519\u4e86\uff0c\u5e94\u8be5\u662f",  # da cuo le, ying gai shi
    "\u7ea0\u6b63\u4e4b\u540e",                    # jiu zheng zhi hou
)
# tl-ego workspace memory db (WORKSPACE = .openclaw/workspace)
_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "memory", "memu.db")
_BGE_URL = "http://127.0.0.1:8002/v1/embeddings"
# 近重复合并阈值 (2026-09-11 实测: 300 条经验里 71% 存在 cos>0.995 的近重复,
# margin 中位 0.0000 -> 不去重的话注入的 3 条可能是同一条的三份拷贝)。1.0=关。
_DEDUPE_COS = float(os.environ.get("EGO_EPI_DEDUPE_COS", "0.995") or 0.995)


_EMB_DIM = 1024
# Last (text, vector) pair — guards against an embedding server that returns a
# stale/cached vector. NOTE (2026-09-15, retracted diagnosis): the 650 identical
# epi vectors were NOT caused by this — those 650 texts were byte-identical on
# input (see _episodic_text below). The guard is still worth having.
_LAST_EMB = {"text": None, "vec": None}


def _embedding(text):
    """Return a 1024-d embedding vector, or None on ANY failure.

    Callers already treat None as 'embedding_unavailable' (neural_direct_gate,
    _episodic_recall) and every call site sits inside a blanket try/except, so
    raising here just turned failures into a silent 'no answer'. Honouring the
    None contract in this one place covers all five call sites.
    """
    import requests
    try:
        r = requests.post(_BGE_URL, json={"input": text[:6000], "model": "default"},
                          timeout=10, proxies={"http": None, "https": None})
        r.raise_for_status()
        v = r.json()["data"][0]["embedding"]
    except Exception:
        return None
    if not isinstance(v, (list, tuple)) or len(v) != _EMB_DIM:
        print(f"[embed] unexpected shape ({type(v).__name__}, "
              f"{len(v) if hasattr(v, '__len__') else '?'}) -> rejected", flush=True)
        return None
    if (_LAST_EMB["vec"] is not None and text != _LAST_EMB["text"]
            and list(v) == _LAST_EMB["vec"]):
        print("[embed] identical vector for a different text -> embedding "
              "server degraded, rejecting", flush=True)
        return None
    _LAST_EMB["text"], _LAST_EMB["vec"] = text, list(v)
    return v


def _tokens(s):
    import re as _re
    cjk = _re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
    toks = set()
    for raw in (s or "").split():
        tok = raw.strip(".,!?;:'\"<>()[]{}|`~@#$%^&*+-=_")
        if len(tok) < 2:
            continue
        if cjk.search(tok):
            toks.add(tok)
            for i in range(len(tok) - 1):
                toks.add(tok[i:i + 2])
        else:
            toks.add(tok.lower())
            if len(tok) > 2:
                toks.add(tok[:3].lower())
    return toks


def _nums(s):
    import re as _re
    return set(_re.compile(r"[0-9\uff10-\uff19]+").findall(s or ""))


def neural_direct_gate(query, s1_neural_report=""):
    """Pure-local gate. Returns audit dict; 'fired' decides."""
    if _DISABLED or not query or len(query.strip()) < 4:
        return {"fired": False, "reason": "disabled_or_short"}
    t0 = time.time()
    try:
        import sqlite3
        conn = sqlite3.connect(_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, summary, content, memory_type, confidence, recalls "
            "FROM memory_items "
            "WHERE confidence >= 0.75 AND (summary IS NOT NULL OR content IS NOT NULL) "
            "ORDER BY confidence DESC, recalls DESC LIMIT 3000"
        ).fetchall()
        conn.close()
        if not rows:
            return {"fired": False, "reason": "no_candidates"}

        qemb = _embedding(query)
        if qemb is None:
            return {"fired": False, "reason": "embedding_unavailable"}

        qtoks = _tokens(query)
        qnums = _nums(query)
        cands = [dict(r) for r in rows]
        for c in cands:
            text_all = (c.get("summary") or "") + " " + (c.get("content") or "")
            c["_overlap"] = len(qtoks & _tokens(text_all))
            c["_is_correction"] = any(m in text_all for m in _MARKERS)
            c["_num_guard"] = bool(qnums and not (qnums & _nums(text_all)))
            c["_score"] = c["_overlap"] + (2.0 if (c["_is_correction"] and c["_overlap"] >= 1) else 0.0)
        top = sorted(cands, key=lambda c: c["_score"], reverse=True)[:20]
        if not top or top[0]["_score"] < 1.0:
            return {"fired": False, "reason": "no_token_overlap"}

        scored = []
        for c in top[:5]:
            text = ((c.get("summary") or "") + " " + (c.get("content") or ""))[:6000]
            e = _embedding(text)
            if e is None:
                continue
            dot = sum(a * b for a, b in zip(qemb, e))
            na = sum(a * a for a in qemb) ** 0.5
            nb = sum(b * b for b in e) ** 0.5
            scored.append((dot / (na * nb) if na and nb else 0.0, c))
        if not scored:
            return {"fired": False, "reason": "embed_rows_failed"}
        scored.sort(key=lambda t: t[0], reverse=True)
        cos, best = scored[0]

        fired = (best["_is_correction"] and cos >= 0.80
                 and best["_overlap"] >= 2 and not best["_num_guard"])
        audit = {
            "fired": fired,
            "sim": round(cos, 4),
            "row_id": best.get("id"),
            "row_type": best.get("memory_type"),
            "is_correction": best["_is_correction"],
            "overlap": best["_overlap"],
            "num_guard": best["_num_guard"],
            "took_ms": int((time.time() - t0) * 1000),
        }
        if fired:
            ans = (best.get("content") or best.get("summary") or "").strip()
            audit["answer"] = ans
            audit["answer_len"] = len(ans)
        return audit
    except Exception as e:
        return {"fired": False, "reason": "error:" + type(e).__name__, "detail": str(e)[:160]}


# === Episodic memory gate (2026-09-08) ======================================
# Generalize NeuralDirect from correction-rows-only to ALL action memories.
# Source: .lae/tool_executions.db (the write funnel already records every
# tool execution there). Zero changes to the correction gate above.
_EPISODB = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lae", "tool_executions.db")
_EPISODB_MTIME = 0.0
_EPISODIC_CACHE = []  # [(row, vec)] incremental: keyed by memory_id, new rows only
_EPISODIC_SEEN = set()  # memory_ids already embedded
_EPISODIC_ORDER = []  # memory_ids in insertion order (ring eviction)


_NOISE_TOOLS = ("check_status", "mc_world_state")  # pure polls, zero memory value


def _episodic_rows(limit=400):
    import sqlite3
    conn = sqlite3.connect(_EPISODB)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT memory_id, tool, args, result, source, ts, iso_ts, error "
            "FROM tool_executions ORDER BY rowid DESC LIMIT ?", (limit * 3,)
        ).fetchall()]
    finally:
        conn.close()
    # info filter: status polls are near-identical rows that inflate every
    # query's cosine baseline (NEG tests hit 0.67 on them) — keep them out of
    # the index entirely. Keep last poll of each as a low-value fallback.
    kept, seen_poll = [], 0
    for r in rows:
        if r.get("tool") in _NOISE_TOOLS:
            seen_poll += 1
            if seen_poll % 10 != 1:  # keep every 10th poll only
                continue
        kept.append(r)
        if len(kept) >= limit:
            break
    return kept


def _episodic_text(row):
    # ponytail: raw tool JSON is noisy for BGE; distill the signal fields.
    import json as _json
    args = (row.get("args") or "").strip()
    res = (row.get("result") or "").strip()
    err = (row.get("error") or "").strip()
    tail = (" ERR=" + err) if err else ""
    summary = res[:300]
    try:
        d = _json.loads(res)
        bits = []
        for k in ("status", "position", "bot_online", "held_item", "query",
                  "matches", "health", "food", "nearby_entities", "error"):
            if k in d:
                bits.append("%s=%s" % (k, _json.dumps(d[k], ensure_ascii=False)[:120]))
        if bits:
            # 追加原始 summary 而不是替换。白名单漏掉 destination / command /
            # server_reply 等字段时(如 move),替换会把**不同的**调用压成同一个
            # 字符串 -> 同一个 embedding -> 克隆神经元。2026-09-15 实测: 650 条
            # 不同 ts 的 move 全部蒸馏成同一个 72 字符文本。
            summary = " ".join(bits) + " " + res[:300]
    except Exception:
        pass
    return "[%s] args=%s result=%s%s" % (row.get("tool"), args[:200], summary, tail)


def _episodic_vecs_for(rows):
    """Incremental write hook: embed ONLY new rows (by memory_id); reuse cached
    vecs for everything else. mtime check is just a cheap skip when nothing
    was written. Ring-evicts oldest beyond _EPISODIC_MAX."""
    global _EPISODIC_CACHE, _EPISODB_MTIME, _EPISODIC_ORDER
    _EPISODIC_MAX = 400
    try:
        mtime = os.path.getmtime(_EPISODB)
    except OSError:
        mtime = 0.0
    if mtime == _EPISODB_MTIME and _EPISODIC_CACHE:
        return _EPISODIC_CACHE
    _EPISODB_MTIME = mtime
    for r in rows:
        mid = r.get("memory_id")
        if not mid or mid in _EPISODIC_SEEN:
            continue
        try:
            v = _embedding(_episodic_text(r))
        except Exception:
            continue
        if v is None:
            continue
        _EPISODIC_CACHE.append((r, v))
        _EPISODIC_SEEN.add(mid)
        _EPISODIC_ORDER.append(mid)
    # ring eviction (both index structures shrink together)
    while len(_EPISODIC_ORDER) > _EPISODIC_MAX:
        old = _EPISODIC_ORDER.pop(0)
        _EPISODIC_SEEN.discard(old)
        _EPISODIC_CACHE[:] = [(rr, vv) for rr, vv in _EPISODIC_CACHE if rr.get("memory_id") != old]
    return _EPISODIC_CACHE


def episodic_gate(query, top_k=3, min_cos=0.55):
    """NeuralDirect generalization (2026-09-08): when the correction gate did
    NOT fire, surface relevant PAST EXPERIENCES from tool_executions.db.
    Returns {'fired': bool, 'context': str, 'hits': [...], 'took_ms': int}.
    The context is injected into the LLM prompt as [Past Experience] — it does
    NOT bypass the LLM (action logs are evidence, not restatable answers).
    2026-09-09: fired hits are queued for n-epi-* neuron binding (recall IS
    consolidation). Fail-safe: any exception -> fired=False."""
    t0 = time.time()
    try:
        qemb = _embedding(query)
        if qemb is None:
            return {"fired": False, "reason": "embedding_unavailable",
                    "took_ms": int((time.time() - t0) * 1000)}
        # coarse: wide cosine net (BGE baseline inflation makes tight cos useless)
        hits = _episodic_search(query, top_k=max(top_k * 2, 6), min_cos=0.60)
        if not hits:
            return {"fired": False, "reason": "no_hits",
                    "took_ms": int((time.time() - t0) * 1000)}
        # fine: LLM judge returns p(relevant) floats (jev-cal 2026-09-20):
        # >=0.55 keep, 0.40-0.55 keep ranked below confident ones (soft inject
        # via ordering), <0.40 drop.
        probs = _judge_batch(query, [_episodic_text(h) for h in hits])
        _log_calibration(query, hits, probs)
        kept = [(h, p) for h, p in zip(hits, probs) if p >= _JP_MID]
        if not kept:
            return {"fired": False, "reason": "judge_rejected_all",
                    "took_ms": int((time.time() - t0) * 1000)}
        kept.sort(key=lambda t: t[1], reverse=True)
        hits = [h for h, _ in kept]
        # recall-as-consolidation: queue fired experiences for binding.
        # fix: bind the EXPERIENCE's own vector (not the query's) so each
        # n-epi-* neuron has a distinct embedding in the substrate.
        try:
            rows_by_id = {r.get("memory_id"): (r, v) for r, v in _episodic_vecs_for(_episodic_rows())}
            for h in hits:
                _pair = rows_by_id.get(h.get("memory_id"))
                if _pair is not None:
                    queue_episodic_binding(_pair[0], _pair[1])
        except Exception:
            pass
        return {
            "fired": True,
            "hits": len(hits),
            "top_sim": hits[0].get("_sim"),
            "context": _episodic_format(hits, max_chars=600),
            "took_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        return {"fired": False, "reason": "error:" + type(e).__name__,
                "detail": str(e)[:120], "took_ms": int((time.time() - t0) * 1000)}


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _dedupe_hits(scored, top_k=5, dedupe_cos=0.995):
    """scored: [(sim, row, vec)] 已按 sim 降序 -> 去近重复后的 top_k。

    与已选中的任一条 cos >= dedupe_cos 就视为同一条: 不占名额, 只把 _copies +1。
    这样 3 条注入额度花在 3 条**不同**经验上, 而不是同一条的三份拷贝。
    """
    if dedupe_cos >= 1.0:             # 1.0 = 关闭去重 (完全相同的向量 cos 也是 1.0)
        dedupe_cos = float("inf")
    picked = []                       # [(sim, row, vec, copies)]
    for s, r, v in scored:
        if len(picked) >= top_k:
            break
        for i, (ps, pr, pv, pc) in enumerate(picked):
            if _cos(v, pv) >= dedupe_cos:
                picked[i] = (ps, pr, pv, pc + 1)
                break
        else:
            picked.append((s, r, v, 1))
    out = []
    for s, r, v, c in picked:
        rr = dict(r)                  # 别改缓存里的 row (缓存跨调用复用)
        rr["_sim"] = round(s, 4)
        rr["_copies"] = c
        out.append(rr)
    return out


def _episodic_search(query, top_k=5, min_cos=0.45, dedupe_cos=None):
    rows = _episodic_rows()
    if not rows:
        return []
    qemb = _embedding(query)
    if qemb is None:
        return []
    scored = [(_cos(qemb, v), r, v) for r, v in _episodic_vecs_for(rows)]
    scored = [s for s in scored if s[0] >= min_cos]
    scored.sort(key=lambda t: t[0], reverse=True)
    _dc = _DEDUPE_COS if dedupe_cos is None else dedupe_cos
    return _dedupe_hits(scored, top_k=top_k, dedupe_cos=_dc)


def _episodic_format(rows, max_chars=500):
    parts = []
    used = 0
    for r in rows:
        line = "%s%s %s %s: %s" % (
            r.get("_sim", ""), ("x%d" % r["_copies"]) if r.get("_copies", 1) > 1 else "",
            (r.get("iso_ts") or "")[:16],
            r.get("tool"), _episodic_text(r)[:200])
        if used + len(line) > max_chars:
            break
        parts.append(line)
        used += len(line)
    return "\n".join(parts)


# === Episodic neuron binding (2026-09-09) ====================================
# Level-1 neuro-binding: surfaced experiences get bound to dedicated
# n-epi-* neurons in the substrate. neural_direct only QUEUES candidates;
# the server-side worker (tl_ego_server._episodic_binding_worker) owns all
# global state (NEURON_EMBED/NEURONS/EDGE_ACTIVE/EdgeStoreV2).
_BIND_QUEUE = []          # [{"memory_id", "text", "vec"}] FIFO, bounded
_BIND_QUEUE_MAX = 300
_BIND_BINDABLE = set()    # memory_ids already queued (dedupe)
_BIND_TEXTS = set()       # text digests already queued (content dedupe)


def queue_episodic_binding(row, vec=None):
    """Enqueue an experience for neuron binding. Called on gate FIRE (recall)
    and by tool_router's write funnel (auto-binding). vec may be None — the
    server-side worker then embeds the text (keeps the action path zero-latency).
    Bounded FIFO + dedupe: the ring _EPISODIC_CACHE eviction already forgets
    the oldest; the queue just mirrors what is still worth binding."""
    mid = row.get("memory_id")
    if not mid or mid in _BIND_BINDABLE or len(_BIND_QUEUE) >= _BIND_QUEUE_MAX:
        return False
    # same noise filter as the search index: status polls have no memory value
    if row.get("tool") in _NOISE_TOOLS:
        return False
    _text = _episodic_text(row)[:1000]
    # Content dedupe. memory_id is a fresh uuid4 per call (tool_router.py:80), so
    # the memory_id set above NEVER fires — the same experience got bound again
    # and again. 2026-09-15: 650 identical move neurons, all cos 1.0, which
    # flattened the graph's weight distribution.
    # ponytail: unbounded set, same growth profile as _BIND_BINDABLE above.
    _digest = hashlib.sha1(_text.encode("utf-8", "ignore")).digest()
    if _digest in _BIND_TEXTS:
        return False
    _BIND_QUEUE.append({
        "memory_id": mid,
        "text": _text,
        "vec": [float(x) for x in vec] if vec is not None else None,
    })
    _BIND_BINDABLE.add(mid)
    _BIND_TEXTS.add(_digest)
    return True


# === Cross-encoder style relevance judge (2026-09-09; cloud 2026-09-15) ======
# BGE-M3 cosine cannot separate related/unrelated for short Chinese texts
# (measured: POS 0.65-0.70 vs NEG 0.65-0.68 — no threshold works), so coarse
# cosine hits get re-judged by an LLM. Was a local Gemma-4 E4B llama-server on
# :8003; since 2026-09-15 ONE batched call per retrieval to the shared ego
# cloud endpoint (EGO_LLM_*, same convention as stepfun_tool_caller) behind the
# cross-process 429 gate. Local server + its autostart were removed.
# Judge failure -> drop all candidates (conservative, same as before).
# EGO_EPI_JUDGE=0 (or no key) -> judge skipped, coarse hits pass through.
_JUDGE_OFF = os.environ.get("EGO_EPI_JUDGE", "1") == "0"
_JUDGE_PROMPT_HEAD = (
    "你是记忆检索相关性判断器。判断\"记录\"是否包含能回答\"问题\"的信息。\n"
    "记录是Ego的工具调用日志(tool名称+args参数+result结果), 判断其中的内容主题是否与问题相关。\n"
    "规则: 记录的主题与问题主题一致(如建筑/死亡/位置/聊天) -> \"相关\"; "
    "主题无关或只是空洞状态 -> \"无关\"。\n"
    "示例1: 问题=\"Ego盖过什么房子\" 记录=\"[chat] 聊天:仓库建造完成,27座建筑\" -> 相关\n"
    "示例2: 问题=\"1+1等于几\" 记录=\"[check_status] 状态ok\" -> 无关\n"
    "示例3: 问题=\"它死过吗\" 记录=\"[recall_memory] 查询:死亡事件 被杀死\" -> 相关\n"
    "示例4: 问题=\"Ego的建筑历史\" 记录=\"[recall_memory] 查询:建造 建筑 仓库 聚落\" -> 相关\n"
)


_HEAD = _JUDGE_PROMPT_HEAD
_JUDGE_PROMPT_HEAD = (
    _HEAD
    + "\n输出概率: 每条判定后附0到1的小数, 表示你判断该条\"相关\"的把握"
      "(如 1:相关0.9 2:无关0.1)。校准要求: 把握0.9的判断实际应约九成正确。\n"
)

# jev-cal 2026-09-20: judge now returns p(relevant) floats, not bools.
# 0.55+ keep as-is, 0.40-0.55 keep but marked 待定 (soft inject), <0.40 drop.
# ponytail: thresholds hand-picked pending real calibration data in
# .lae/judge_calibration.jsonl — re-fit once a few hundred queries logged.
_JP_KEEP = 0.55
_JP_MID = 0.40
_JP_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lae", "judge_calibration.jsonl")


def _parse_judge_probs(txt, n_docs):
    """Parse '1:相关0.9 2:无关0.1' -> list[float] p(relevant), len n_docs.
    Missing prob -> word default (相关0.9/无关0.1, preserves old bool behavior).
    Missing entry / unparseable -> 0.0 (conservative drop, same as old)."""
    verdict = {int(i): (v == "相关", float(p) if p else (0.9 if v == "相关" else 0.1))
               for i, v, p in re.findall(r"(\d+)\s*[:：.、]?\s*(相关|无关)\s*([01](?:\.\d+)?)?", txt)}
    return [verdict.get(i + 1, (False, 0.0))[1] for i in range(n_docs)]


def _log_calibration(query, hits, probs):
    """One JSONL line per judged query: probs + cos for offline re-fit."""
    try:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "q": (query or "")[:80],
               "ps": [[round(p, 2), round(h.get("_sim") or 0.0, 3), h.get("tool")]
                      for h, p in zip(hits, probs)]}
        with open(_JP_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _judge_batch(query, docs):
    """list[float] p(relevant) aligned to docs (jev-cal 2026-09-20).
    Judge unreachable -> 0.0 per doc (conservative: drop rather than inject
    noise). Judge disabled / no API key -> all 1.0 (coarse gate decides)."""
    if not docs:
        return []
    if _JUDGE_OFF:
        return [1.0] * len(docs)
    key = os.environ.get("EGO_LLM_API_KEY", "") or os.environ.get("STEPFUN_API_KEY", "")
    if not key:
        try:
            _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ego_key.txt")
            key = open(_p, encoding="utf-8", errors="replace").read().splitlines()[0].strip()
        except Exception:
            key = ""
    if not key:
        return [1.0] * len(docs)
    try:
        import re
        from ego_llm_gate import llm_post_json
        numbered = "\n".join(f"{i + 1}. {(d or '')[:250]}" for i, d in enumerate(docs))
        prompt = (_JUDGE_PROMPT_HEAD + "问题: " + query + "\n候选记录:\n" + numbered +
                  "\n逐条判断, 只输出一行, 格式严格如: 1:相关0.9 2:无关0.1 3:相关0.85")
        r = llm_post_json(
            os.environ.get("EGO_LLM_URL", "https://api.stepfun.com/step_plan/v1/chat/completions"),
            headers={"Authorization": "Bearer " + key},
            json={"model": os.environ.get("EGO_LLM_MODEL", "step-3.5-flash"),
                  "messages": [{"role": "user", "content": prompt}],
                  # step-3.5-flash reasons by default and burns the token budget
                  # on hidden reasoning (content="") -> force minimal effort.
                  # 2026-09-23: reasoning_effort=minimal does NOT stop the
                  # reasoning; it only shrinks it. 384 tokens is consumed
                  # entirely by it -> finish_reason=length, content="" ->
                  # _parse_judge_probs -> all 0.0 = every episode dropped.
                  # Same class of bug as hy-memory's maxTokens<4096 (see
                  # MEMORY.md). Verified live: 384 -> content='', 4096 ->
                  # content='1:无关0.1 2:无关0.1' (finish stop).
                  "reasoning_effort": "minimal",
                  "max_tokens": 4096, "temperature": 0.0},
            timeout=30)
        txt = r.json()["choices"][0]["message"]["content"] or ""
        return _parse_judge_probs(txt, len(docs))
    except Exception:
        return [0.0] * len(docs)


# ==== 2026-10-14 决策轮记忆注入 (重建 09-24 设计, 两端: recent_failures + lessons) ====
# 触发点: stepfun_tool_caller 的 MC 决策分支 (query 以 "Ego:(" 开头, tools=[] 的
# 文本决策轮)。两个函数都零 throw、毫秒级、不碰网络 — 每轮决策都能安全调用。
_DB_URI = "file:" + os.path.abspath(_DB).replace("\\", "/") + "?mode=ro"
_LESSONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lae", "lessons.json")
_LESSONS_CACHE = {"mtime": 0.0, "note": ""}


def recent_failures_note(top_k=3):
    """[上次怎么栽的] — deterministic SQL over memu mc_activity/mc_lesson。
    deaths ranked above tool-argument rejections (2026-09-24 设计)。"""
    try:
        import sqlite3
        con = sqlite3.connect(_DB_URI, uri=True, timeout=3)
        try:
            cur = con.cursor()
            deaths = cur.execute(
                "SELECT summary FROM memory_items WHERE memory_type IN ('mc_activity','mc_lesson')"
                " AND (summary LIKE '%被%击杀%' OR summary LIKE '%被%杀死%')"
                " ORDER BY ts DESC LIMIT ?", (top_k,)).fetchall()
            need = top_k - len(deaths)
            rej = []
            if need > 0:
                rej = cur.execute(
                    "SELECT summary FROM memory_items WHERE memory_type IN ('mc_activity','mc_lesson')"
                    " AND (summary LIKE '%被拒绝%' OR summary LIKE '%failed%')"
                    " ORDER BY ts DESC LIMIT ?", (need,)).fetchall()
        finally:
            con.close()
        items = []
        for (s,) in list(deaths) + list(rej):
            s = (s or "").strip()
            if s and s not in items:
                items.append(s[:70])
        return ("[上次怎么栽的] " + " | ".join(items)) if items else ""
    except Exception:
        return ""


def standing_lessons_note(max_rules=5):
    """[站着的教训] — .lae/lessons.json (mtime 缓存, reject:* 类不注入:
    bridge arg-format bug 不是决策 LLM 能修的)。"""
    try:
        mt = os.path.getmtime(_LESSONS)
        if mt != _LESSONS_CACHE["mtime"]:
            _LESSONS_CACHE["mtime"] = mt
            data = json.load(open(_LESSONS, encoding="utf-8"))
            rules = []
            for L in (data.get("lessons") or []):
                if not isinstance(L, dict):
                    continue
                cls = str(L.get("class") or "")
                rule = str(L.get("rule") or "").strip()
                if rule and not cls.startswith("reject:"):
                    rules.append(rule)
            _LESSONS_CACHE["note"] = (("[站着的教训 — 已沉淀的规则, 优先于临时起意] " +
                                       " ; ".join(rules[:max_rules]))[:400]) if rules else ""
        return _LESSONS_CACHE["note"]
    except Exception:
        return ""
