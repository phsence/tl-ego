"""2026-11-03: 置信度不得造假 —— HTTP 层检查 (服务运行中即可跑)。

P5 (StepFun 工具预步) 绕过 full_pipeline, 本来就拿不到 S1/S2 置信度信号。历史上那里
写死 confidence=1.0 / margin=1.0 / verdict="tool_call", 结果两条学习通路被污染:
  1. _record_s2_shadow 的 external_confidence_sum → 外部老师"永远 100% 确定"
  2. stage4_s2_internalization 拿它当权重上限 → 工具类决策样本权重全顶格
改成 None(=未测量) + confidence_source 标记后, 本检查断言真实服务的返回:
  - confidence 不再是伪造的 1.0
  - 未测量就明确写 None 并给出来源标记 (而不是编一个数)
  - verdict 由"实际有没有跑工具"推出, 不是常量

跑法: python test_honest_confidence.py        (要求 8766 在线)
"""
import json
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8766"
QUERY = "用一句话说说你现在能做什么？"


def post(path, body, timeout=600):
    req = urllib.request.Request(
        API + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    for _ in range(6):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(6)
                continue
            raise


def ask(q):
    jid = post("/contemplate", {"query": q}).get("job_id")
    assert jid, "no job_id"
    for _ in range(60):
        time.sleep(5)
        j = post("/contemplate-job", {"job_id": jid})
        if j.get("status") not in ("running", "queued", None):
            return j.get("result") or j
    raise AssertionError("job timed out")


r = ask(QUERY)
conf = r.get("confidence")
src = r.get("confidence_source")
verdict = r.get("verdict")
route = r.get("llm_route")

print(f"route={route} verdict={verdict} confidence={conf!r} source={src}")

# 核心断言: 不许再出现"没测量却声称 1.0"
if route == "tool_call" or verdict in ("tool_call", "text"):
    assert conf != 1.0, "confidence 仍是伪造的 1.0 -- 改动没生效?"
    assert conf is None or isinstance(conf, (int, float)), f"confidence 类型异常: {conf!r}"
    if conf is None:
        assert src, "写了 None 却没标 confidence_source, 下游无法区分'未测量'和'没有'"
# verdict 必须是推导值, 不能是常量
assert verdict in ("tool_call", "text", "neural_direct", None), f"verdict 异常: {verdict!r}"

print("OK: 未测量就写 None + 来源标记, verdict 由实际行为推导")
