#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TL-Ego Phase 1+2: 双系统联合
  Phase 1 (升级): 用 route_with_attractor 做 System 1 直觉
  Phase 2: LLM 推理 System 2 + Aggregator 合并

Usage: python tl_ego_phase1_2.py [--queries N]
"""

import sys, json, time, os, argparse
import requests, numpy as np
from pathlib import Path
from collections import Counter

SKILL_DIR = Path(r"C:\Users\Administrator\.openclaw\skills\neural_grid")
sys.path.insert(0, str(SKILL_DIR))

DATA_DIR = Path(r"C:\Users\Administrator\.openclaw\workspace\neural_grid_data_100k_backup")

import neural_grid as ng
ng.DATA_DIR = DATA_DIR
ng.NEURON_REGISTRY = DATA_DIR / "neuron_registry.jsonl"
ng.GRID_EDGES_DB = DATA_DIR / "grid_edges.sqlite"
ng.EdgeStore._instance = None
ng._edge_store = None

# ─── API endpoints ───
BGE_URL = "http://127.0.0.1:8002/v1/embeddings"
LLM_URL = "http://127.0.0.1:8001/v1/chat/completions"

TYPE_ORDER = ["sensory", "integrator", "decider", "action", "memory", "rule_modification"]

def parse_nid_type(nid: str) -> str:
    parts = nid.split("-")
    return parts[1] if len(parts) >= 2 and parts[1] in TYPE_ORDER else "unknown"

def get_embedding(text: str) -> list:
    r = requests.post(BGE_URL, json={"input": text, "model": "default"}, timeout=30)
    return r.json()["data"][0]["embedding"]

def llm_reason(query: str, context: dict = None) -> dict:
    """System 2: LLM reasoning. Returns structured analysis."""
    sys_prompt = """你是一个双系统AI架构中的System 2（分析型推理系统）。
你的任务是：对用户查询进行结构化分析，输出JSON格式的分析报告。

分析维度：
1. query_type: 查询类型 (sensory/integrator/decider/action/memory)
2. confidence: 你对分析的自信度 (0.0-1.0)
3. key_insight: 一句话核心洞察
4. suggested_action: 建议的下一步行动（如果是decider/action类型）
5. reasoning_process: 2-3句话解释你的推理过程

回复格式必须是严格的JSON。不要包含任何其他文本。"""
    
    payload = {
        "model": "default",
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"用户查询: {query}"}
        ],
        "temperature": 0.3,
        "max_tokens": 512
    }
    if context:
        payload["messages"].insert(1, {"role": "system", "content": f"神经网络的直觉信号: {json.dumps(context, ensure_ascii=False)}"})
    
    try:
        r = requests.post(LLM_URL, json=payload, timeout=60)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        # Parse JSON from LLM output (handle potential markdown wrapping)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        if text.startswith("json"):
            text = text[4:].strip()
        return json.loads(text)
    except Exception as e:
        return {"query_type": "unknown", "confidence": 0.0, "key_insight": f"LLM error: {str(e)}", "suggested_action": "none", "reasoning_process": "LLM unavailable"}

def aggregator(sys1: dict, sys2: dict) -> dict:
    """Merge System 1 (attractor) + System 2 (LLM) into final decision."""
    # Confidence merging
    s1_conf = sys1.get("basin_stability", 0.0)
    s2_conf = sys2.get("confidence", 0.0)
    
    # Type agreement
    s1_type = sys1.get("dominant_type", "unknown")
    s2_type = sys2.get("query_type", "unknown")
    type_agreement = (s1_type == s2_type)
    
    # Final confidence = weighted average with bonus for agreement
    if s1_conf >= 0.6 and s2_conf >= 0.6 and type_agreement:
        final_conf = min(1.0, (s1_conf + s2_conf) / 2 + 0.15)
        verdict = "high_confidence_agree"
    elif s1_conf >= 0.5 or s2_conf >= 0.7:
        if type_agreement:
            final_conf = (s1_conf * 0.6 + s2_conf * 0.4)
            verdict = "medium_confidence_agree"
        else:
            # Disagreement: both are confident but disagree - reduce confidence
            final_conf = max(s1_conf, s2_conf) * 0.5
            verdict = "disagreement" if s1_conf >= 0.3 and s2_conf >= 0.3 else "low_confidence"
    else:
        final_conf = max(s1_conf, s2_conf) * 0.4
        verdict = "low_confidence"
    
    # Consensus output
    if type_agreement:
        consensus_type = s1_type
    elif s1_conf > s2_conf:
        consensus_type = s1_type
    else:
        consensus_type = s2_type
    
    return {
        "consensus_type": consensus_type,
        "final_confidence": round(final_conf, 3),
        "verdict": verdict,
        "type_agreement": type_agreement,
        "system_1_stability": round(s1_conf, 3),
        "system_2_confidence": round(s2_conf, 3),
        "system_1_type": s1_type,
        "system_2_type": s2_type,
        "system_1_summary": sys1.get("contemplation_summary", ""),
        "system_2_insight": sys2.get("key_insight", ""),
    }

def analyze_attractor(res: dict) -> dict:
    """Extract type metrics from route_with_attractor output."""
    final_state = res.get("final_state", {})
    if not final_state:
        return {"basin_stability": 0.0, "dominant_type": "unknown", "confidence_signal": "none", 
                "final_type_distribution": {}, "contemplation_summary": "No activation"}
    
    # Type distribution
    tcount = Counter(parse_nid_type(n) for n in final_state)
    total = sum(tcount.values())
    dist = {t: tcount.get(t, 0) for t in TYPE_ORDER}
    
    if total == 0:
        return {"basin_stability": 0.0, "dominant_type": "unknown", "confidence_signal": "none",
                "final_type_distribution": dist, "contemplation_summary": "Zero activation"}
    
    dominant = max(dist, key=dist.get)
    conc = dist[dominant] / total
    
    # Entropy-based competition
    probs = [v/total for v in dist.values() if v > 0]
    entropy = -sum(p * np.log2(p) for p in probs)
    max_entropy = np.log2(len(TYPE_ORDER))
    competition = entropy / max_entropy if max_entropy > 0 else 1.0
    
    active_types = sum(1 for v in dist.values() if v > 0)
    if active_types <= 2: base_stab = 1.0
    elif active_types == 3: base_stab = 0.6
    else: base_stab = 0.3
    stability = max(0.0, min(1.0, base_stab - competition * 0.3))
    
    # Confidence signal
    if conc > 0.6 and stability > 0.6:
        cs = "high"
    elif conc > 0.4 or stability > 0.4:
        cs = "medium"
    elif competition > 0.7:
        cs = "conflict"
    else:
        cs = "low"
    
    return {
        "basin_stability": round(stability, 4),
        "type_concentration": round(conc, 4),
        "competition_level": round(competition, 4),
        "dominant_type": dominant,
        "final_activated_count": len(final_state),
        "final_type_distribution": dist,
        "confidence_signal": cs,
        "contemplation_summary": (
            f"Strong {dominant} attractor ({conc:.0%})" if cs == "high"
            else f"Multiple basins competing" if cs == "conflict"
            else f"Moderate {dominant} attractor ({conc:.0%})"
        )
    }

def phase1_system1(query_embedding: list) -> dict:
    """System 1: Attractor-based intuition (upgraded with route_with_attractor)."""
    t0 = time.time()
    res = ng.route_with_attractor(
        query_embedding,
        top_n=50,
        propagation_rounds=15,
        max_bfs_candidates=1000,
        bfs_depth=2,
        return_details=True,
        learning=False  # No STDP during inference
    )
    dt = time.time() - t0
    analysis = analyze_attractor(res)
    analysis["time_s"] = round(dt, 2)
    analysis["raw_seeds"] = res.get("seeds", [])
    analysis["candidate_count"] = len(res.get("initial_candidates", []))
    return analysis

# ─── Main test harness ───
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=int, default=4)
    args = parser.parse_args()
    
    queries = [
        "房间里能看到什么？",
        "连接这两篇论文的发现",
        "下一步该做什么？",
        "启动服务并打开浏览器",
    ][:args.queries]
    
    print("=" * 70)
    print("  TL-Ego Phase 1+2: Dual System Integration")
    print("  System 1: Attractor Network + System 2: LLM")
    print("=" * 70, flush=True)
    
    # Init grid
    print("\n[Init] Loading 100k Neural Grid...", end=" ", flush=True)
    t0 = time.time()
    ng.init_grid(preload_partitions=False)
    print(f"done ({time.time()-t0:.1f}s)", flush=True)
    
    results = []
    
    for qi, q in enumerate(queries):
        print(f"\n{'─'*70}")
        print(f"  [{qi+1}/{len(queries)}] Query: {q}")
        print(f"{'─'*70}", flush=True)
        
        step_data = {"query": q, "timestamp": time.strftime("%H:%M:%S")}
        
        # Step 1: Embedding
        t0 = time.time()
        emb = get_embedding(q)
        step_data["embedding_time"] = round(time.time() - t0, 2)
        print(f"  [Embed] {len(emb)}d in {step_data['embedding_time']}s", flush=True)
        
        # Step 2: System 1 - Attractor intuition
        t0 = time.time()
        s1 = phase1_system1(emb)
        step_data["system1"] = s1
        s1_time = time.time() - t0
        print(f"  [S1-Net] {s1['time_s']}s | type={s1['dominant_type']} | "
              f"stability={s1['basin_stability']:.3f} | conc={s1['type_concentration']:.3f} | "
              f"conf={s1['confidence_signal']}", flush=True)
        print(f"    Seeds: {s1['raw_seeds']}", flush=True)
        print(f"    Dist: {s1['final_type_distribution']}", flush=True)
        print(f"    Summary: {s1['contemplation_summary']}", flush=True)
        
        # Step 3: System 2 - LLM reasoning
        t0 = time.time()
        s2 = llm_reason(q, context={
            "attractor_type": s1["dominant_type"],
            "stability": s1["basin_stability"],
            "competition": s1["competition_level"],
        })
        step_data["system2"] = s2
        s2_time = time.time() - t0
        print(f"  [S2-LLM] {s2_time:.1f}s | type={s2.get('query_type','?')} | "
              f"conf={s2.get('confidence',0):.2f}", flush=True)
        print(f"    Insight: {s2.get('key_insight','')[:80]}", flush=True)
        print(f"    Action:  {s2.get('suggested_action','')[:80]}", flush=True)
        
        # Step 4: Aggregation
        t0 = time.time()
        agg = aggregator(s1, s2)
        step_data["aggregator"] = agg
        print(f"  [AGG] {time.time()-t0:.2f}s | verdict={agg['verdict']} | "
              f"consensus={agg['consensus_type']} | conf={agg['final_confidence']:.3f}", flush=True)
        print(f"    Agree? {'Y' if agg['type_agreement'] else 'N'} "
              f"S1={agg['system_1_type']} vs S2={agg['system_2_type']}", flush=True)
        
        results.append(step_data)
    
    # Summary
    print(f"\n{'='*70}")
    print("  SUMMARY")
    print(f"{'='*70}")
    agrees = sum(1 for r in results if r["aggregator"]["type_agreement"])
    print(f"  Queries: {len(results)} | Type agreement: {agrees}/{len(results)}")
    avg_conf = np.mean([r["aggregator"]["final_confidence"] for r in results])
    print(f"  Avg final confidence: {avg_conf:.3f}")
    
    # Save report
    report_path = Path(r"C:\Users\Administrator\.openclaw\workspace\reports\tl_ego_phase1_2_report.json")
    report_path.parent.mkdir(exist_ok=True)
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "queries": results,
        "summary": {
            "n_queries": len(results),
            "type_agreements": agrees,
            "avg_final_confidence": round(float(avg_conf), 3),
        }
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n  Report saved: {report_path}", flush=True)
    print(f"{'='*70}\n", flush=True)

if __name__ == "__main__":
    main()
