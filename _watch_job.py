import json, time, urllib.request
JID = "b3798f1f0883"
def q(jid):
    r = urllib.request.Request("http://127.0.0.1:8766/contemplate-job",
                               data=json.dumps({"job_id": jid}).encode(),
                               headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=30).read().decode())
for i in range(90):
    time.sleep(20)
    try:
        d = q(JID)
    except Exception as e:
        print("poll error", e, flush=True); continue
    st = d.get("status")
    if st not in ("running", "queued"):
        res = d.get("result") or {}
        print("DONE status", st, flush=True)
        print("route", res.get("llm_route"), "verdict", res.get("verdict"),
              "confidence", repr(res.get("confidence")), "source", res.get("confidence_source"), flush=True)
        print("answer", str(res.get("response") or "")[:120].replace("\n", " "), flush=True)
        raise SystemExit(0)
print("STILL RUNNING after 30min", flush=True)
