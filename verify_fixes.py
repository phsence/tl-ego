"""TL-Ego Three-Fix Verification"""
import urllib.request, json, statistics

BASE = 'http://127.0.0.1:8766'

def api(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type":"application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=120)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

queries = [
    "今天天气怎么样",
    "帮我写一首关于海的诗歌",
    "分析一下最近的AI发展趋势",
    "执行并记录实验结果",
    "记住这个重要的决策",
    "总结一下刚才的讨论要点",
    "制定一个学习计划",
    "观察并描述当前系统状态",
    "从记忆中检索相关的历史信息",
    "综合这些观点得出一个结论",
    "决定下一步行动优先级",
    "感知并报告当前GPU负载",
    "检索关于TL-Ego的所有记录",
    "整合不同来源的信息",
    "执行一个快速原型测试",
    "存储这个关键洞察到记忆",
    "回顾最近7天的任务进展",
    "综合S1和S2的结果做判断",
    "感知系统中的异常情况",
    "检索与Karpathy相关的指导原则",
]

# Fix 1: S2 Type Diversity
print("=== Fix 1: S2 Type Diversity (n=%d) ===" % len(queries))
s2_types = {}
for q in queries:
    r = api("POST", "/contemplate", {"query": q, "depth": 1})
    if "error" in r:
        print("  ERROR:", r["error"])
        continue
    s2 = r["system2"]["type"]
    s2_types[s2] = s2_types.get(s2, 0) + 1
    agg = r["aggregator"]
    bar = "Y" if agg["agreement"] else "N"
    print("  %-12s | %-35s | conf=%.3f agree=%s" % (s2, agg["verdict"], agg["final_confidence"], bar))

print("\nS2 type distribution:")
for t, c in sorted(s2_types.items(), key=lambda x:-x[1]):
    print("  %-12s: %d (%.0f%%)" % (t, c, c/len(queries)*100))
print("Types observed:", len(s2_types), "(target >= 3)")

# Fix 2: Confidence Spread
print("\n=== Fix 2: Confidence Spread ===")
conf_by_v = {}
agree_count = 0
disagree_count = 0
for q in queries:
    r = api("POST", "/contemplate", {"query": q, "depth": 1})
    if "error" in r:
        continue
    v = r["aggregator"]["verdict"]
    c = r["aggregator"]["final_confidence"]
    conf_by_v[v] = conf_by_v.get(v, []) + [c]
    if r["aggregator"]["agreement"]:
        agree_count += 1
    else:
        disagree_count += 1

print("agree_rate: %d/%d = %.0f%%" % (agree_count, len(queries), agree_count/len(queries)*100))
print("disagree_rate: %d/%d = %.0f%%" % (disagree_count, len(queries), disagree_count/len(queries)*100))
print("\nConfidence by verdict type:")
for v, clist in sorted(conf_by_v.items(), key=lambda x: -len(x[1])):
    avg = sum(clist)/len(clist)
    mn = min(clist)
    mx = max(clist)
    print("  %-35s: avg=%.3f min=%.3f max=%.3f n=%d" % (v, avg, mn, mx, len(clist)))

all_confs = [c for cl in conf_by_v.values() for c in cl]
std_c = statistics.stdev(all_confs) if len(all_confs) > 1 else 0
print("\nOverall confidence std:", "%.4f" % std_c, "(target: >0.05)")

# Fix 3: Metacontemplate
print("\n=== Fix 3: Metacontemplate ===")
s = api("GET", "/status")
print("  metacontemplate_runs:", s.get("metacontemplate_runs", "?"))

# Summary
print("\n" + "="*55)
print("  FIX VERIFICATION SUMMARY")
print("="*55)
fix1_ok = len(s2_types) >= 3
fix2_ok = std_c > 0.05
fix3_ok = s.get("metacontemplate_runs", 0) > 0
print("  Fix 1 (S2 diversity >=3 types):  %s  (got %d)" % ("PASS" if fix1_ok else "FAIL", len(s2_types)))
print("  Fix 2 (conf std > 0.05):         %s  (got %.4f)" % ("PASS" if fix2_ok else "FAIL", std_c))
print("  Fix 3 (meta count > 0):          %s  (got %d)" % ("PASS" if fix3_ok else "FAIL", s.get("metacontemplate_runs", 0)))
print("="*55)
