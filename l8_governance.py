"""l8_governance.py — REBUILT 2026-10-01 (source lost in the 09-30 wipe).

Provenance: every constant and string below is recovered from a surviving
artifact or a call site — nothing is invented.
  * `.lae/l8/governance_log.jsonl` (21069 real rows) — the 8-field row schema
    (ts, proposal_id, proposal_type, target_module, description, decision,
    reason, confidence), the decision vocabulary, and the exact reason strings.
  * `.lae/l8/governance_stats.json` — the get_stats() key set, including the
    dual `approved`/`approve` + `flagged`/`flag` pairs that L9 reads.
  * `l9_autonomy_monitor.py:129-134` — confirms `total_reviewed` / `approved`
    / `approve` are the keys it consumes.
  * `_bak_domfix2_20260914/tl_ego_server.py.pre2:10001-10136` — the two
    call sites' contracts: `create_governance()`, `review_batch(list) -> list
    of {"decision","reason"}`, `get_stats()`, and `L8Governance(WORKSPACE)`.
  * Thresholds measured from the log, not guessed: every one of the 1098 denials
    says "large change (500 lines)", and all 115 low-risk approvals sit at
    10-30 lines.

Judgement (not measured): the original's risk classifier. The four rules below
reproduce 100% of the observed decisions; the ordering between them is a
reconstruction. Proposals whose risk string is unknown are denied rather than
approved — the conservative direction, and the one that matches every
high-risk row on record.
"""

import json
import os
import re
import threading
from datetime import datetime

# ── measured thresholds ──────────────────────────────────────────────────────
DOC_ONLY_MAX_LINES = 0        # a proposal with no code is doc-only at any size
DENY_LINES = 500              # every denial on record is exactly this
LOW_RISK_MAX_LINES = 30       # low-risk approvals on record span 10-30

# An unsubstituted placeholder in the code body: the proposal names a symbol it
# never resolved, so it cannot be applied as written. Measured 12/159 of the
# replayable historical approvals (all WIRE_MODULE, literal `<core_function>`).
_PLACEHOLDER_RE = re.compile(r"<[a-z_][a-z0-9_]*>", re.IGNORECASE)

REASON_DOC_ONLY = ("Doc-only proposal (%s) — no executable patch; "
                   "auto-approving to clear draft backlog.")
REASON_MEDIUM = "Medium risk (medium) — flagging for staged rollout with observation window."
REASON_LOW = "Low risk (low), %d lines — auto-approving."
REASON_DENY = "High risk (high) or large change (%d lines) requires human review."

CONF_DOC_ONLY = 0.92          # 2083+5165 doc-only approvals: all 0.92
CONF_LOW = 0.95               # 115 low-risk approvals: all 0.95
CONF_MEDIUM = 0.70            # 10790 flags: all exactly 0.70
CONF_DENY = 0.90              # 1098 denials: all exactly 0.90

L8_DIR = os.path.join("l8",)
LOG_NAME = "governance_log.jsonl"
STATS_NAME = "governance_stats.json"

# The review loop calls `create_governance()` with no arguments, while the
# per-tick stats reader constructs `L8Governance(WORKSPACE)`. Both must land on
# the same `.lae/l8/` ledger, so the factory resolves the workspace itself.
# Resolution order: OPENCLAW_WORKSPACE (the env tl-ego already sets), then the
# known layout <...>/workspace/projects/tl-ego -> <...>/workspace.
_DEFAULT_WORKSPACE = None


def _default_workspace():
    global _DEFAULT_WORKSPACE
    if _DEFAULT_WORKSPACE:
        return _DEFAULT_WORKSPACE
    env = os.environ.get("OPENCLAW_WORKSPACE")
    if env and os.path.isdir(env):
        _DEFAULT_WORKSPACE = env
        return env
    here = os.path.dirname(os.path.abspath(__file__))          # .../projects/tl-ego
    cand = os.path.normpath(os.path.join(here, "..", ".."))     # .../workspace
    _DEFAULT_WORKSPACE = cand if os.path.isdir(cand) else here
    return _DEFAULT_WORKSPACE


def create_governance(workspace=None):
    """Factory used by the L7/L8 review loop (no-arg call site)."""
    return L8Governance(workspace or _default_workspace())


def _now():
    return datetime.now().isoformat()


def _is_doc_only(prop):
    """Doc-only == the proposal ships nothing that could actually be applied.

    Anchor: every doc-only approval names its type in the reason ("no executable
    patch"). Two shapes satisfy that, both measured against the 159 replayable
    historical approvals:
      1. proposed_code empty, or every non-blank line is a comment/TODO/docstring.
      2. proposed_code contains an unsubstituted `<placeholder>` token
         (`from X import <core_function>`) — a body that cannot be applied even
         in principle, because the real symbol was never filled in.
    Without (2) the rule misses 12 of 159 real approvals (all WIRE_MODULE with a
    literal `<core_function>`); with it, 159/159. The test keys on the code body,
    never on proposal type: WIRE_MODULE appears as 5165 approve and 8070 flag.
    """
    code = prop.get("proposed_code")
    if not isinstance(code, str) or not code.strip():
        return True
    if _PLACEHOLDER_RE.search(code):
        return True
    for line in code.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and s not in ('"""', "'''") and not s.upper().startswith("TODO"):
            return False   # found real code
    return True


def _lines(prop):
    try:
        return int(prop.get("est_lines") or 0)
    except (TypeError, ValueError):
        return 0


def _risk(prop):
    r = prop.get("risk")
    return r.strip().lower() if isinstance(r, str) else ""


class L8Governance:
    """Append-only review ledger for L7 self-modification proposals.

    Two call sites:
      * `create_governance()` — the review loop (`review_batch`).
      * `L8Governance(WORKSPACE)` — the per-tick stats reader (`get_stats`).
    Both are satisfied by this one class; WORKSPACE is accepted and ignored
    because the ledger path is fixed to `<workspace>/.lae/l8/`.
    """

    def __init__(self, workspace=None):
        self.dir = os.path.join(str(workspace), ".lae", "l8") if workspace else os.path.join(".lae", "l8")
        self.log_path = os.path.join(self.dir, LOG_NAME)
        self.stats_path = os.path.join(self.dir, STATS_NAME)
        self._lock = threading.Lock()
        self._stats = None

    # ── public API ──────────────────────────────────────────────────────────
    def review_batch(self, proposals):
        """Review a list of L7 proposal dicts.

        Returns one decision dict per input, in order:
            {"decision": "approve"|"flag"|"deny", "reason": str,
             "confidence": float, "proposal_id": str, "ts": str}
        """
        out = []
        for prop in (proposals or []):
            out.append(self._review_one(prop))
        return out

    def review_one(self, prop):
        return self._review_one(prop)

    def get_stats(self):
        """Counters for the L9 monitor. Keys are the union the log proves:
        total_reviewed / approved / approve / flagged / flag / denied / deny.
        Dual spellings are load-bearing: L9 reads `approved` but falls back to
        `approve`, and both appear on record.
        """
        counts = self._load_stats()
        counts.setdefault("approved", counts.get("approve", 0))
        counts.setdefault("approve", counts.get("approved", 0))
        counts.setdefault("flagged", counts.get("flag", 0))
        counts.setdefault("flag", counts.get("flagged", 0))
        counts.setdefault("denied", counts.get("deny", 0))
        counts.setdefault("deny", counts.get("denied", 0))
        counts["total_reviewed"] = counts.get("approved", 0) + counts.get("flagged", 0) + counts.get("deny", 0)
        counts["last_updated"] = self._log_mtime_iso()
        return counts

    # ── decision rule ───────────────────────────────────────────────────────
    def _review_one(self, prop):
        prop = prop or {}
        pid = str(prop.get("id") or "?")
        ptype = prop.get("type") or prop.get("proposal_type") or "UNKNOWN"
        lines = _lines(prop)
        risk = _risk(prop)

        if _is_doc_only(prop):
            decision, reason, conf = "approve", REASON_DOC_ONLY % (ptype,), CONF_DOC_ONLY
        elif risk == "low" and lines <= LOW_RISK_MAX_LINES:
            decision, reason, conf = "approve", REASON_LOW % (lines,), CONF_LOW
        elif risk in ("high", "") or lines >= DENY_LINES:
            decision, reason, conf = "deny", REASON_DENY % (lines,), CONF_DENY
        else:
            decision, reason, conf = "flag", REASON_MEDIUM, CONF_MEDIUM

        row = {
            "ts": _now(),
            "proposal_id": pid,
            "proposal_type": ptype,
            "target_module": prop.get("target_module"),
            "description": prop.get("description"),
            "decision": decision,
            "reason": reason,
            "confidence": conf,
        }
        self._append(row)
        return row

    # ── ledger I/O ──────────────────────────────────────────────────────────
    def _append(self, row):
        """Atomic-ish append: rewrite stats via tmp+replace, append one log line."""
        with self._lock:
            try:
                os.makedirs(self.dir, exist_ok=True)
                counts = self._load_stats()
                key = {"approve": "approved", "flag": "flagged", "deny": "denied"}[row["decision"]]
                counts[key] = int(counts.get(key, 0)) + 1
                counts["last_updated"] = row["ts"]
                counts["rebuilt_from_log"] = False
                tmp = self.stats_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(counts, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, self.stats_path)
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception as e:
                # never let bookkeeping break the review loop
                print("[l8-governance] append failed: %s" % e, flush=True)

    def _load_stats(self):
        if self._stats is not None:
            return self._stats
        counts = {"approved": 0, "flagged": 0, "denied": 0}
        try:
            with open(self.stats_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                counts.update({k: v for k, v in loaded.items() if isinstance(v, int)})
        except Exception:
            # stats file gone or corrupt -> rebuild from the append-only log,
            # which is the durable record (measured 21069 rows -> exact counts)
            counts = self._rebuild_from_log()
        return counts

    def _rebuild_from_log(self):
        counts = {"approved": 0, "flagged": 0, "denied": 0}
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue  # torn last line from a hard kill
                    key = {"approve": "approved", "flag": "flagged", "deny": "denied"}.get(d.get("decision"))
                    if key:
                        counts[key] += 1
            counts["rebuilt_from_log"] = True
        except FileNotFoundError:
            pass
        return counts

    def _log_mtime_iso(self):
        try:
            return datetime.fromtimestamp(os.path.getmtime(self.log_path)).isoformat()
        except OSError:
            return _now()

    def get_stats_for_l9(self):
        """Convenience: the exact shape L9AutonomyMonitor.check_loop expects."""
        return self.get_stats()


if __name__ == "__main__":
    import tempfile

    # 1. decision table matches every rule observed in the real ledger
    doc = {"id": "t1", "type": "GENERIC_FIX", "risk": "medium", "est_lines": 10,
           "proposed_code": "# only comments\n"}
    low = {"id": "t2", "type": "FIX", "risk": "low", "est_lines": 15,
           "proposed_code": "print(1)\n"}
    med = {"id": "t3", "type": "WIRE_MODULE", "risk": "medium", "est_lines": 15,
           "proposed_code": "print(1)\n"}
    big = {"id": "t4", "type": "DECOMPOSE_SERVER", "risk": "high", "est_lines": 500,
           "proposed_code": "print(1)\n"}
    empty = {"id": "t5", "type": "X", "risk": "low", "est_lines": 5, "proposed_code": ""}
    # an unsubstituted placeholder is unapplicable, so it is doc-only even though
    # it carries code and would otherwise be flagged at medium risk
    ph = {"id": "t6", "type": "WIRE_MODULE", "risk": "medium", "est_lines": 15,
          "proposed_code": "try:\n    from tool_router import <core_function>\n    <core_function>(RESTING_STATE)\nexcept ImportError:\n    pass\n"}

    with tempfile.TemporaryDirectory() as tmp:
        gov = L8Governance(tmp)
        dec = gov.review_batch([doc, low, med, big, empty, ph])
        got = [(d["decision"], d["confidence"]) for d in dec]
        want = [("approve", 0.92), ("approve", 0.95), ("flag", 0.70),
                ("deny", 0.90), ("approve", 0.92), ("approve", 0.92)]
        assert got == want, "decision table: got %r want %r" % (got, want)
        assert all(len(d["reason"]) > 10 for d in dec), "reasons must be specific"

        # 2. the ledger is durable and get_stats() satisfies L9's dual keys
        s = gov.get_stats()
        assert s["total_reviewed"] == 6, s
        for k in ("approved", "approve", "flagged", "flag", "denied", "deny"):
            assert k in s, "L9 needs key %r" % k
        assert s["approved"] == 4 and s["flagged"] == 1 and s["denied"] == 1, s

        # 3. stats file deleted -> rebuilt from the append-only log
        os.remove(os.path.join(tmp, ".lae", "l8", "governance_stats.json"))
        gov._stats = None
        s2 = gov.get_stats()
        assert s2["total_reviewed"] == 6 and s2["approved"] == 4, s2
        assert s2["rebuilt_from_log"] is True, s2

        # 4. degenerate inputs must not raise
        assert gov.review_batch([]) == []
        assert gov.review_batch(None) == []
        assert gov.review_batch([{}])[0]["decision"] == "approve"  # no code = doc-only
        assert gov.review_batch([{"risk": "low", "est_lines": "not-an-int"}])[0]["decision"] in ("approve", "flag", "deny")

        # 5. the factory must resolve the REAL workspace ledger (the server calls
        #    it with no args), and it must resolve to the same path the class
        #    would get when handed that same workspace.
        real_log = os.path.join(_default_workspace(), ".lae", "l8", "governance_log.jsonl")
        before = os.path.getsize(real_log) if os.path.exists(real_log) else 0
        fac = create_governance()
        real_ws = _default_workspace()
        assert fac.log_path == L8Governance(real_ws).log_path, (fac.log_path, real_ws)
        assert fac.log_path == real_log, (fac.log_path, real_log)
        assert os.path.isabs(fac.log_path) and fac.log_path.endswith(
            os.path.join(".lae", "l8", "governance_log.jsonl")), fac.log_path
        assert hasattr(fac, "review_batch") and hasattr(fac, "get_stats")
        # constructing it must not touch the real ledger
        after = os.path.getsize(real_log) if os.path.exists(real_log) else 0
        assert before == after, "constructing the factory wrote to the real ledger"

        # 6. the real ledger parses under this schema
        real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "..", ".lae", "l8", "governance_log.jsonl")
        real = os.path.normpath(real)
        if os.path.exists(real):
            n = 0
            with open(real, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    assert set(d.keys()) == {"ts", "proposal_id", "proposal_type",
                                             "target_module", "description",
                                             "decision", "reason", "confidence"}, d
                    assert d["decision"] in ("approve", "flag", "deny"), d
                    n += 1
            assert n == 21069, "expected 21069 real rows, read %d" % n
            print("l8_governance selfcheck OK | real ledger: %d rows verified" % n)

    print("l8_governance selfcheck OK | 5 rules + rebuild + degenerate inputs")
