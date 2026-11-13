import json, time, urllib.request

JOB = "70ccb28e66a3"
URL = "http://127.0.0.1:8766/contemplate-job"


def post(payload):
    req = urllib.request.Request(URL, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


deadline = time.time() + 420
last = None
while time.time() < deadline:
    try:
        d = post({"job_id": JOB})
    except Exception as e:
        print("poll_err", repr(e)[:120]); time.sleep(10); continue
    st = d.get("status")
    if st != last:
        print("status", st, flush=True); last = st
    if st and st != "running":
        res = d.get("result") or d
        te = res.get("tool_executions")
        print("tool_executions", json.dumps(te, ensure_ascii=False)[:800] if te else "NONE")
        r = (res.get("response") or "")
        print("response_head", r[:400].replace("\n", " "))
        open(r"C:/Users/Administrator/.openclaw/workspace/projects/tl-ego/_ct_video_result.json", "w", encoding="utf-8").write(
            json.dumps(d, ensure_ascii=False, indent=1)[:200000])
        break
    time.sleep(15)
else:
    print("timeout waiting for job")
