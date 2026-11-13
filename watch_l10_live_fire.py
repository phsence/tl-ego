"""Watch the LIVE L10 loop for the first fire that carries a real family.

Background proof that the qd_saturation fix landed in the running system: the
ego server reloads the generator per tick, so the next trigger (tick >= 70653)
must emit kwta_threshold_expansion / hp_types_rotation / dead_type_booster_floor
/ curiosity_activation_recovery instead of the algorithm_selection dead end.

Polls .lae/l10_gen_audit.jsonl (the loop's own log — no forcing, no writes).
Exits 0 on match, 1 after timeout.
"""
import json
import sys
import time
from pathlib import Path

LAE = Path(r"C:/Users/Administrator/.openclaw/workspace/.lae")
AUDIT = LAE / "l10_gen_audit.jsonl"
DEAD_END = {"algorithm_selection", "cross_system_perspective"}
TIMEOUT_S = 4 * 3600


def last_row() -> dict | None:
    try:
        raw = AUDIT.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    idx = raw.rfind("\n{")
    chunk = raw[idx + 1:] if idx >= 0 else raw
    try:
        return json.loads(chunk)
    except Exception:
        return None


def main() -> int:
    base_tick = (last_row() or {}).get("tick")
    print(f"watching live L10 fires (baseline tick {base_tick})", flush=True)
    t0 = time.time()
    while time.time() - t0 < TIMEOUT_S:
        row = last_row() or {}
        tick = row.get("tick")
        if tick and base_tick and tick > base_tick:
            rationale = str(row.get("meta_rationale") or "")
            dead = any(d in rationale for d in DEAD_END) or row.get("candidate_count", 0) == 0
            print(f"new fire tick={tick} candidates={row.get('candidate_count')} "
                  f"rationale={rationale[:120]}", flush=True)
            if not dead:
                print("LIVE_OK: real family emitted by the running loop", flush=True)
                return 0
            base_tick = tick
        time.sleep(60)
    print("TIMEOUT: no real-family fire within 4h", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
