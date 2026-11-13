#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Independent delivery verifier for TL-Ego commander messages.

Trigger (2026-10-11, user request "根据ego的自主"): NOT an external scheduler.
The stage1 consequence sensor spawns main() on ego's own resting-cycle gate
(stage1_consequence_sensor._spawn_delivery_verifier, ~every 120s of ego time),
so ego notices its own messages landing as part of sensing its consequences.
Manual run stays possible: `python ego_comm_delivery_verifier.py`

Why: L5 comm goals land in the ledger as `unmeasured:success`
(plugin_watcher feedback carries evidence_valid=false by design). But the
delivery itself IS independently checkable: the host watcher writes
delivery_receipt.json AND appends the message to .lae/commander_inbox.jsonl.
This module cross-checks both artifacts and posts ONE /feedback row per
delivered goal with source=independent_verifier + evidence_valid=true, so the
goal's newest terminal row carries real delivery evidence instead of
bookkeeping (server whitelist: stage1_consequence_sensor / independent_verifier
/ external_episode_ledger are the trusted measured sources).

Contract: verify BOTH (a) receipt delivered=true with matching goal_id and
(b) a commander_inbox line for the same goal_id. If either is missing -> SKIP
(never convert a claim into a success).
Dedup: scans l5_goals.jsonl for existing judge=delivery_receipt rows; rerun-safe.
Bounded: newest LIMIT sandboxes per run, receipts older than MAX_AGE_S ignored.

ponytail: full-file scans are fine at current sizes (ledger ~17k lines, inbox
~400); add cursors if either grows 10x. stdout is quiet when idle.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WORKSPACE = Path(r"C:/Users/Administrator/.openclaw/workspace")
SAND = WORKSPACE / "ego_sandbox"
INBOX = WORKSPACE / ".lae" / "commander_inbox.jsonl"
LEDGER = WORKSPACE / ".lae" / "l5_goals.jsonl"
SKIP_LOG = WORKSPACE / ".lae" / "comm_delivery_skip.log"
BASE = "http://127.0.0.1:8766"
LIMIT = 10
MAX_AGE_S = 3 * 86400
JUDGE = "delivery_receipt"


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def already_verified():
    done = set()
    try:
        for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("judge") == JUDGE and o.get("goal_id"):
                done.add(str(o["goal_id"]))
    except OSError:
        pass
    return done


def inbox_goals():
    seen = {}
    try:
        for line in INBOX.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue
            gid = str(o.get("goal_id") or "")
            if gid and o.get("text"):
                seen[gid] = str(o.get("ts") or "")
    except OSError:
        pass
    return seen


def _seen_before(tag):
    try:
        return tag in SKIP_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _mark_seen(tag):
    try:
        with open(SKIP_LOG, "a", encoding="utf-8") as f:
            f.write(tag + "\n")
    except OSError:
        pass


def post_feedback(body):
    req = urllib.request.Request(
        BASE + "/feedback",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main():
    done = already_verified()
    inbox = inbox_goals()
    verified, problems = [], []
    for sd in sorted(SAND.glob("l5g-*"), reverse=True):
        if len(verified) + len(problems) >= LIMIT:
            break
        gid = sd.name
        if gid in done:
            continue
        rp = sd / "delivery_receipt.json"
        if not rp.is_file():
            continue
        try:
            if time.time() - rp.stat().st_mtime > MAX_AGE_S:
                continue
        except OSError:
            continue
        r = _read_json(rp)
        if not isinstance(r, dict) or r.get("delivered") is not True:
            continue
        if str(r.get("goal_id") or gid) != gid:
            problems.append((gid, "receipt goal_id mismatch"))
            continue
        in_ts = inbox.get(gid)
        if not in_ts:
            problems.append((gid, "receipt delivered but inbox line missing"))
            continue
        body = {
            "task_id": gid,
            "goal_id": gid,
            "outcome": "success",
            "judge": JUDGE,
            "reason": "independent_delivery_verification",
            "source": "independent_verifier",
            "evidence_valid": True,
            "tool": "commander_message",
            "action": "commander_delivery",
            "evidence": (
                "independent check ok: commander_inbox line present "
                f"(ts={in_ts}); receipt delivered_at={r.get('delivered_at')} "
                f"session={r.get('session_key')} receipt_reason={r.get('reason')}"
            )[:500],
        }
        try:
            payload = post_feedback(body)
            if payload.get("measured"):
                verified.append(gid)
            else:
                problems.append((gid, "server reported measured=False: "
                                 + json.dumps(payload, ensure_ascii=False)[:160]))
        except Exception as e:
            problems.append((gid, f"POST failed: {e}"))

    # stdout policy: silent when idle; one compact line per real event.
    if verified:
        tail = ", ".join(verified[-4:])
        print(f"✅ TL-Ego 投递独立验证: {len(verified)} 条已入账 ({tail})")
    for gid, why in problems:
        tag = f"PROBLEM|{gid}|{why}"
        if not _seen_before(tag):
            _mark_seen(tag)
            print(f"⚠️ TL-Ego 投递验证异常 {gid}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
