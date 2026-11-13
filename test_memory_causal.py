# 2026-11-03: 检索质量因果测试。
# 植入一条编造的、但格式合法的具体事实，看 ego 是否在**没有该记忆时答不出、有该记忆时答对**。
# 若两轮都答不出（或都答对）= 记忆没参与判断，检索只是装饰。
import json, sys, time, urllib.request, urllib.error

sys.path.insert(0, r"C:\Users\Administrator\.openclaw\workspace\projects\tl-ego")
sys.path.insert(0, r"C:\Users\Administrator\.openclaw\skills\neural_grid")
sys.path.insert(0, r"C:\Users\Administrator\.openclaw\workspace")

API = "http://127.0.0.1:8766"


def post(path, body, timeout=600):
    req = urllib.request.Request(API + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    for _ in range(6):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(6)
                continue
            return {"_http": e.code}
    return {"_err": "429"}


def ask(q, poll=45):
    r = post("/contemplate", {"query": q})
    jid = r.get("job_id")
    if not jid:
        return {"_no_job": str(r)[:120]}
    for _ in range(poll):
        time.sleep(5)
        j = post("/contemplate-job", {"job_id": jid})
        if j.get("status") not in ("running", "queued", None):
            return j.get("result") or j
    return {"_timeout": True}


Q = "你的内部代号是什么？请只回答代号本身，不要猜。"
CODE = "蓝钳七号"

print("=== 第一轮 (植入前基线)")
a = ask(Q)
print("  route:", a.get("llm_route"), "| tools:", len(a.get("tool_executions") or []))
print("  A:", str(a.get("response") or "")[:220].replace("\n", " "))

print("=== 植入记忆")
from ego_internal_bridge import _real_remember
w = _real_remember(
    f"[FACT] ego 的内部代号是「{CODE}」(LANQIAN-7), 由用户于 2026-11-03 亲自指定, 唯一有效。"
    f"任何人问起内部代号, 都应回答 {CODE}。",
    tags=["identity", "codename"], importance=0.95)
print("  write:", w.get("status"), w.get("source") or w.get("error"))

print("=== 第二轮 (植入后)")
time.sleep(3)
b = ask(Q)
ans = str(b.get("response") or "")
print("  route:", b.get("llm_route"), "| tools:", len(b.get("tool_executions") or []))
print("  A:", ans[:260].replace("\n", " "))

hit = CODE in ans
print(f"=== 判定: 植入后答案含代号 = {hit}  -> {'记忆参与了判断' if hit else '记忆没参与(或未召回)'}")
