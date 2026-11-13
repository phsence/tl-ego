"""Commander outbox watcher — Hermes cron delivery with watermark.

Pure stdlib script. Reads .lae/outbox/user_reports.jsonl, prints rows newer
than the watermark (.lae/outbox/last_delivered.json), then advances it.
Empty output when nothing new — safe for cron watchdog pattern.

Usage:
    python commander_outbox_watcher.py            # print undelivered, advance watermark
    python commander_outbox_watcher.py --status   # show watermark only, no mutation
"""
import json
import sys
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
OUTBOX = WORKSPACE / ".lae" / "outbox" / "user_reports.jsonl"
WATERMARK = WORKSPACE / ".lae" / "outbox" / "last_delivered.json"


def read_watermark():
    try:
        return json.loads(WATERMARK.read_text(encoding="utf-8")).get("last_ts", "")
    except Exception:
        return ""


def write_watermark(ts):
    WATERMARK.parent.mkdir(parents=True, exist_ok=True)
    WATERMARK.write_text(json.dumps({"last_ts": ts, "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                    ensure_ascii=False), encoding="utf-8")


def main():
    if "--status" in sys.argv:
        print(f"watermark={read_watermark() or '(none)'}")
        return
    last = read_watermark()
    if not OUTBOX.exists():
        return  # silent: nothing ever delivered
    rows = []
    try:
        with open(OUTBOX, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        print(f"[commander-watcher] read error: {e}", file=sys.stderr)
        return
    fresh = [r for r in rows if r.get("ts", "") > last]
    if not fresh:
        return  # silent
    # terminal events merged into one digest; accepted noted briefly
    terminals = [r for r in fresh if r.get("event") == "terminal"]
    accepted = [r for r in fresh if r.get("event") == "accepted"]
    if accepted:
        print(f"[指挥官任务受理] {len(accepted)} 项:")
        for r in accepted:
            print(f"  - {r.get('goal_id')} | {r.get('task','')[:60]}")
    if terminals:
        print(f"[指挥官任务完成] {len(terminals)} 项:")
        for r in terminals:
            print(f"  - {r.get('goal_id')} | {r.get('task','')[:50]} | "
                  f"结果={r.get('outcome')} | {r.get('report','')[:150]}")
    # advance watermark to the newest delivered row
    newest = max(r.get("ts", "") for r in fresh)
    write_watermark(newest)


if __name__ == "__main__":
    main()
