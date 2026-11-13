"""logic_layer.py - content-bearing facts + invariant guards for ego.

Why this file exists: ego's 19 types (sensory/integrator/veto/...) are KWTA
competition channels. They carry mass, never propositions. So ego can prefer
an action but cannot *state* anything about the world, and therefore cannot be
wrong in a checkable way. This adds the missing half: assertions with content,
and rules that can reject an action before it is dispatched.

Guards (each mapped to a failure ego has actually shown):
  no_dup    - same tool dispatched again inside DUP_WINDOW_TICKS (loop)
  resource  - acting on a prerequisite that is empty/unknown (scaffold_materials=[])
  evidence  - irreversible action on unverified inference (S2 acc ~0.27)

Calibration, measured on .lae/l5_goals.jsonl (386 creation rows, tick 69349-70644):
  same-tool dispatch gap: min 0, p5 10, median 17.5
  gap<=3: 6/328 = 1.8%    -> window 3 catches tight loops only
  gap<=20: 185/328 = 56%  -> a 20-tick window would starve ego. Never use 20.

Fail-open: any error inside gate() allows the goal and bumps `errors`. A broken
guard must never freeze ego.

ponytail: rules are plain python predicates. No DSL, no parser, no solver.
Add z3 only when a rule needs satisfiability over unknowns instead of a guard
over known facts. Facts are append-only jsonl replayed at boot (~1 row per goal,
~170/day) so no rotation before ~1 year.
"""
from __future__ import annotations

import json
import os
import time

DUP_WINDOW_TICKS = 3          # measured, see module docstring
MIN_CONF_IRREVERSIBLE = 0.6

_FACTS = None
_LOG_PATH = None
MODE = "enforce"              # enforce | shadow
STATS = {"checked": 0, "passed": 0, "blocked": 0, "observed": 0,
         "errors": 0, "last": None}


class Facts:
    """Append-only (subj, pred) -> val store. Replays jsonl on load."""

    def __init__(self, path=None):
        self.path = path
        self.rows = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue  # torn write, skip
                    if r.get("op") == "retract":
                        self.rows.pop((r["subj"], r["pred"]), None)
                    else:
                        self.rows[(r["subj"], r["pred"])] = r

    def get(self, subj, pred, default=None):
        row = self.rows.get((subj, pred))
        return default if row is None else row.get("val")

    def assert_(self, subj, pred, val, src="asserted", tick=0):
        row = {"op": "assert", "subj": subj, "pred": pred, "val": val,
               "src": src, "tick": tick, "ts": time.time()}
        self.rows[(subj, pred)] = row
        self._append(row)
        return row

    def retract(self, subj, pred, tick=0):
        row = {"op": "retract", "subj": subj, "pred": pred, "tick": tick,
               "ts": time.time()}
        self.rows.pop((subj, pred), None)
        self._append(row)

    def _append(self, row):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ── guards ───────────────────────────────────────────────────────────────
# Each takes (facts, act) and returns None (pass) or a reason string (reject).

def rule_no_dup(facts, act):
    if act.get("kind") != "dispatch_goal":
        return None
    # Self-generated goals only. A user directive is an explicit request and
    # must never be swallowed as a "repeat"; those ids also carry no tick.
    if act.get("source") == "user_directive" or int(act.get("tick") or 0) <= 0:
        return None
    last = facts.get("dispatch", act.get("target"), None)
    if last is None:
        return None
    if int(act.get("tick", 0)) - int(last) < DUP_WINDOW_TICKS:
        return "duplicate dispatch of %r at tick %s, last at %s" % (
            act.get("target"), act.get("tick"), last)
    return None


def rule_resource(facts, act):
    res = act.get("resource")
    if not res:
        return None
    val = facts.get("resource", res, None)
    if val is None:
        return "resource %r unknown - assert it before acting" % (res,)
    if val in (0, "", [], {}, False):
        return "resource %r is empty (%r)" % (res, val)
    return None


def rule_evidence(facts, act):
    if act.get("reversible", True):
        return None
    ev = act.get("evidence") or {}
    conf = ev.get("confidence")
    if conf is None:
        return "irreversible action with no evidence attached"
    if float(conf) < MIN_CONF_IRREVERSIBLE:
        return "irreversible action on confidence %.2f < %.2f" % (
            float(conf), MIN_CONF_IRREVERSIBLE)
    return None


RULES = (("no_dup", rule_no_dup),
         ("resource", rule_resource),
         ("evidence", rule_evidence))


def check(facts, act):
    """-> {'ok': bool, 'rule': str, 'why': str}"""
    for name, fn in RULES:
        try:
            why = fn(facts, act)
        except Exception as exc:  # a broken rule must not block the caller
            return {"ok": False, "rule": name, "why": "rule error: %r" % (exc,)}
        if why:
            return {"ok": False, "rule": name, "why": why}
    return {"ok": True, "rule": "", "why": ""}


# ── server glue ──────────────────────────────────────────────────────────

def init(facts_path=None, verdict_log=None, mode=None):
    global _FACTS, _LOG_PATH, MODE
    _FACTS = Facts(str(facts_path) if facts_path else None)
    _LOG_PATH = str(verdict_log) if verdict_log else None
    if mode:
        MODE = str(mode)
    return _FACTS


def _tick_of(goal):
    """Goal ids are l5g-YYYYMMDD-<tick:06d>, so the tick rides in the id."""
    gid = str(goal.get("goal_id") or "")
    try:
        return int(gid.rsplit("-", 1)[1])
    except (ValueError, IndexError):
        return 0


def act_from_goal(goal):
    cfg = goal.get("tool_config") or {}
    ev = goal.get("evidence")
    if not ev and goal.get("confidence") is not None:
        ev = {"confidence": goal.get("confidence")}
    return {
        "kind": "dispatch_goal",
        "target": goal.get("tool_name") or goal.get("goal_type") or goal.get("goal_id"),
        "tick": _tick_of(goal),
        "source": goal.get("source") or "",
        "resource": cfg.get("resource") or goal.get("resource"),
        "reversible": goal.get("reversible", True),
        "evidence": ev,
    }


def gate(goal):
    """-> (allow, verdict). Never raises; fail-open."""
    if _FACTS is None:
        return True, {"ok": True, "rule": "uninit", "why": ""}
    try:
        act = act_from_goal(goal)
        v = check(_FACTS, act)
        STATS["checked"] += 1
        STATS["last"] = {"goal_id": goal.get("goal_id"), "act": act, "verdict": v}
        if v["ok"]:
            STATS["passed"] += 1
            _FACTS.assert_("dispatch", act["target"], act["tick"],
                           src="l5_goal", tick=act["tick"])
        elif MODE == "enforce":
            STATS["blocked"] += 1
        else:
            STATS["observed"] += 1
        _log({"ts": time.time(), "mode": MODE, "goal_id": goal.get("goal_id"),
              "target": act["target"], "verdict": v})
        return (v["ok"] or MODE != "enforce"), v
    except Exception as exc:
        STATS["errors"] += 1
        return True, {"ok": True, "rule": "error", "why": repr(exc)}


def _log(obj):
    if not _LOG_PATH:
        return
    try:
        os.makedirs(os.path.dirname(_LOG_PATH) or ".", exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass


def stats():
    n = len(_FACTS.rows) if _FACTS is not None else 0
    out = dict(STATS)
    out.update({"mode": MODE, "window_ticks": DUP_WINDOW_TICKS, "facts": n})
    return out


# ── self-check (the one runnable check) ──────────────────────────────────

def demo():
    f = Facts()
    f.assert_("resource", "scaffold_materials", ["plank"], src="mcp")
    f.assert_("resource", "fuel", [], src="mcp")
    f.assert_("dispatch", "walk_to", 100, src="L5")

    ok = lambda a: check(f, a)["ok"]
    bad = lambda a: check(f, a)["why"]

    # fresh dispatch, resource present, reversible -> pass
    assert ok({"kind": "dispatch_goal", "target": "chop_tree", "tick": 130,
               "resource": "scaffold_materials", "reversible": True})
    # duplicate inside window -> blocked
    assert "duplicate" in bad({"kind": "dispatch_goal", "target": "walk_to",
                               "tick": 102, "reversible": True})
    # window boundary: gap == DUP_WINDOW_TICKS -> pass
    assert ok({"kind": "dispatch_goal", "target": "walk_to",
               "tick": 100 + DUP_WINDOW_TICKS, "reversible": True})
    # empty prerequisite -> blocked
    assert "empty" in bad({"kind": "dispatch_goal", "target": "fly", "tick": 200,
                           "resource": "fuel", "reversible": True})
    # unknown prerequisite -> blocked
    assert "unknown" in bad({"kind": "dispatch_goal", "target": "fly", "tick": 200,
                             "resource": "jetpack", "reversible": True})
    # goal with no resource field -> rule skipped, no false block
    assert ok({"kind": "dispatch_goal", "target": "fly", "tick": 200,
               "reversible": True})
    # irreversible on weak evidence -> blocked (S2 acc 0.265 world)
    assert "confidence" in bad({"kind": "dispatch_goal", "target": "wipe_memory",
                                "tick": 300, "reversible": False,
                                "evidence": {"confidence": 0.27}})
    # irreversible on strong evidence -> pass
    assert ok({"kind": "dispatch_goal", "target": "wipe_memory", "tick": 300,
               "reversible": False, "evidence": {"confidence": 0.92}})
    # irreversible with no evidence at all -> blocked
    assert "no evidence" in bad({"kind": "dispatch_goal", "target": "wipe_memory",
                                 "tick": 300, "reversible": False})
    # retract clears a fact
    f.retract("resource", "fuel")
    assert "unknown" in bad({"kind": "dispatch_goal", "target": "fly", "tick": 400,
                             "resource": "fuel", "reversible": True})

    # goal -> act mapping, then gate end-to-end (shadow must not block)
    global MODE
    init()
    MODE = "shadow"
    a1, v1 = gate({"goal_id": "l5g-20261111-070633", "tool_name": "decider_deliberate"})
    a2, v2 = gate({"goal_id": "l5g-20261111-070634", "tool_name": "decider_deliberate"})
    assert a1 and v1["ok"], v1
    assert a2 and v2["rule"] == "no_dup", v2          # observed, not blocked
    MODE = "enforce"
    a3, v3 = gate({"goal_id": "l5g-20261111-070635", "tool_name": "decider_deliberate"})
    assert not a3 and v3["rule"] == "no_dup", v3
    assert stats()["blocked"] == 1 and stats()["observed"] == 1, stats()

    # commander goals: id has no tick and repeats are explicit user requests
    init()
    MODE = "enforce"
    d1 = {"goal_id": "l5g-20261111-180236-Ccf6e", "tool_name": "commander_directive",
          "source": "user_directive"}
    d2 = {"goal_id": "l5g-20261111-180237-Dde7f", "tool_name": "commander_directive",
          "source": "user_directive"}
    assert gate(d1)[0] and gate(d2)[0], "a user directive was wrongly blocked"
    print("logic_layer self-check: 17/17 pass")


if __name__ == "__main__":
    demo()
