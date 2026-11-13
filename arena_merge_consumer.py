"""arena_merge_consumer.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: tl_ego_server.py imports consume_arena_merges and calls it once per tick as
  arena_summary = consume_arena_merges(NEURON_TYPE, NEURON_TYPE_CONFIDENCE)
  applied = arena_summary["applied"]; skipped = ...; for res in arena_summary["results"]:
      res["status"] (strings starting "applied"), res.get("file"), res.get("fork_id"),
      res.get("stats", {}).get("affected_neurons")
  and logs each applied file through _write_consumption_log (so this module must not log
  itself, only apply + report).
Artifact anchor: ego_sandbox/<goal_id>/arena_result.json, real sample (09-29):
  {winner, final_dominant, scores{stability,diversity,predictability,progress,total},
   unique_types_visited, avg_concentration, final_stagnation, switches,
   trajectory_summary:[{step, dominant, concentration, stagnation}, ...]}
The server's consumption convention is a ".consumed" sibling marker (used for
ego_self_evidence.json.consumed), reused here.
Judgement (not measured): the exact original priors/merge rule is unrecoverable; this
applies a bounded confidence nudge to the neurons of the winner's visited types and never
rewrites NEURON_TYPE itself.
"""

import json
import os
from pathlib import Path

CONF_NUDGE = 0.02
MAX_CONF = 1.0


def _workspace():
    ws = os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("EGO_WORKSPACE")
    return Path(ws) if ws else Path(__file__).resolve().parents[2]


def _candidate_files(ws):
    out = []
    for pat in ("ego_sandbox/*/arena_result.json", ".lae/arena_merges/*.json"):
        out.extend(sorted(ws.glob(pat)))
    return out


def consume_arena_merges(neuron_type=None, neuron_type_confidence=None):
    """Apply unconsumed arena results to the live type maps. Returns the summary the
    caller logs: {"applied": n, "skipped": n, "results": [{status, fork_id, file, stats}]}"""
    nt = neuron_type if isinstance(neuron_type, dict) else {}
    conf = neuron_type_confidence if isinstance(neuron_type_confidence, dict) else {}
    results = []
    applied = skipped = 0
    try:
        files = _candidate_files(_workspace())
    except Exception as e:
        return {"applied": 0, "skipped": 0, "results": [{"status": "scan_error: %s" % e, "file": None}]}
    for path in files:
        marker = path.with_suffix(path.suffix + ".consumed")
        entry = {"fork_id": path.parent.name, "file": str(path), "status": "skipped", "stats": {}}
        if marker.exists():
            entry["status"] = "skipped_already_consumed"
            skipped += 1
            results.append(entry)
            continue
        try:
            raw = path.read_bytes()
            if not raw.strip(b"\x00"):
                raise ValueError("empty/zero-filled file")
            data = json.loads(raw.decode("utf-8", "replace"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            visited = []
            for step in (data.get("trajectory_summary") or [])[:40]:
                t = step.get("dominant") if isinstance(step, dict) else None
                if t and t not in visited:
                    visited.append(t)
            if not visited and data.get("final_dominant"):
                visited = [data["final_dominant"]]
            if not visited:
                entry["status"] = "no_type_priors"
                skipped += 1
                results.append(entry)
                continue
            touched = 0
            for nid, ntype in nt.items():
                if ntype in visited:
                    cur = conf.get(nid)
                    cur = 0.0 if not isinstance(cur, (int, float)) else float(cur)
                    conf[nid] = min(MAX_CONF, round(cur + CONF_NUDGE, 4))
                    touched += 1
            marker.write_text(json.dumps({"ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
                                          "fork_id": entry["fork_id"], "winner": data.get("winner"),
                                          "types": visited, "affected_neurons": touched}),
                              encoding="utf-8")
            entry["status"] = "applied" if touched else "applied_no_matching_neurons"
            entry["stats"] = {"affected_neurons": touched, "types": visited, "winner": data.get("winner")}
            applied += 1
        except Exception as e:
            entry["status"] = "skipped_error: %s" % str(e)[:80]
            skipped += 1
        results.append(entry)
    return {"applied": applied, "skipped": skipped, "results": results}


if __name__ == "__main__":
    import tempfile, time
    ws = Path(tempfile.mkdtemp())
    os.environ["OPENCLAW_WORKSPACE"] = str(ws)
    (ws / "ego_sandbox" / "l5g-test").mkdir(parents=True)
    (ws / "ego_sandbox" / "l5g-test" / "arena_result.json").write_text(json.dumps(
        {"winner": "mutator", "final_dominant": "integrator",
         "trajectory_summary": [{"step": 0, "dominant": "integrator"}, {"step": 1, "dominant": "action"}]}),
        encoding="utf-8")
    (ws / "ego_sandbox" / "l5g-bad").mkdir(parents=True)
    (ws / "ego_sandbox" / "l5g-bad" / "arena_result.json").write_bytes(b"\x00" * 40)
    nt = {"n1": "integrator", "n2": "action", "n3": "memory"}
    conf = {"n1": 0.5}
    s = consume_arena_merges(nt, conf)
    assert s["applied"] == 1 and s["skipped"] == 1, s
    assert conf["n1"] == 0.52 and conf["n2"] == 0.02 and "n3" not in conf, conf
    assert s["results"][0]["status"].startswith("skipped"), s["results"][0]["status"]
    applied_row = [r for r in s["results"] if r["status"].startswith("applied")][0]
    assert applied_row["stats"]["affected_neurons"] == 2, applied_row
    s2 = consume_arena_merges(nt, conf)
    assert s2["applied"] == 0 and conf["n1"] == 0.52, (s2, conf)
    assert consume_arena_merges(None, None)["applied"] == 0
    print("arena_merge_consumer selfcheck OK")
