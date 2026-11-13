#!/usr/bin/env bash
# TL-Ego daily evaluation script
# Runs the capability benchmark and reports results

set -euo pipefail

WORKSPACE="$(cd "$(dirname "$0")/.." && pwd)"
TL_EGO_DIR="$WORKSPACE/projects/tl-ego"
LOG_DIR="$WORKSPACE/.lae"

echo "=== TL-Ego Daily Evaluation ==="
echo "Date: $(date -Iseconds)"
echo ""

# 1. Check if the server is running
echo "1. Checking server status..."
if ! curl -s --max-time 5 http://127.0.0.1:8766/status > /dev/null 2>&1; then
    echo "ERROR: TL-Ego server is not running on port 8766"
    exit 1
fi
echo "   Server is running."

# 2. Run the capability benchmark
echo ""
echo "2. Running capability benchmark..."
BENCHMARK_OUTPUT="$LOG_DIR/capability_benchmark_$(date +%Y%m%d_%H%M%S).json"
if "$TL_EGO_DIR/capability_benchmark_runner.py" --runs 10 --output "$BENCHMARK_OUTPUT"; then
    echo "   Benchmark completed. Output: $BENCHMARK_OUTPUT"
else
    echo "   WARNING: Benchmark had errors. Check output."
fi

# 3. Check candidate registry
echo ""
echo "3. Checking candidate registry..."
python3 -c "
import sqlite3, json, time
conn = sqlite3.connect('$LOG_DIR/candidate_registry.sqlite')
rows = conn.execute('SELECT state, COUNT(*) FROM candidate_registry GROUP BY state').fetchall()
print('   Registry state counts:')
for state, count in rows:
    print(f'     {state}: {count}')
conn.close()
" 2>/dev/null || echo "   WARNING: Could not read registry"

# 4. Check evolution trigger status
echo ""
echo "4. Checking evolution trigger status..."
STATUS=$(curl -s --max-time 5 http://127.0.0.1:8766/status)
echo "$STATUS" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'   dead_type_count: {d.get(\"dead_type_count\", \"N/A\")}')
print(f'   stagnation_streak: {d.get(\"stagnation_streak\", \"N/A\")}')
print(f'   resting_ticks: {d.get(\"resting_ticks\", \"N/A\")}')
trigger = d.get('evolution_trigger_state', {})
print(f'   evolution_trigger: should={trigger.get(\"should\")}, reason={trigger.get(\"reason\", \"N/A\")}')
" 2>/dev/null || echo "   WARNING: Could not parse status"

# 5. Check for recent evolution rounds
echo ""
echo "5. Checking recent evolution rounds..."
if [ -f "$LOG_DIR/evolution_round_state.json" ]; then
    python3 -c "
import json
with open('$LOG_DIR/evolution_round_state.json') as f:
    d = json.load(f)
print(f'   round: {d.get(\"round\", \"N/A\")}')
print(f'   baseline: {d.get(\"baseline\", {}).get(\"name\", \"N/A\")}')
print(f'   candidates: {len(d.get(\"candidates\", []))}')
print(f'   next_baseline: {d.get(\"next_baseline\", {}).get(\"name\", \"N/A\")}')
"
else
    echo "   No evolution round state found."
fi

# 6. Check sandbox activity
echo ""
echo "6. Checking sandbox activity..."
SANDBOX_COUNT=$(ls -d $LOG_DIR/l8_sandbox/l8g-* 2>/dev/null | wc -l)
echo "   L8 sandbox entries: $SANDBOX_COUNT"

# 7. Check for any errors in the last hour
echo ""
echo "7. Checking recent errors..."
if [ -f "$LOG_DIR/tl_ego_stdout.log" ]; then
    RECENT_ERRORS=$(grep -i "error\|exception\|traceback" "$LOG_DIR/tl_ego_stdout.log" 2>/dev/null | tail -5 | wc -l)
    echo "   Recent error lines: $RECENT_ERRORS"
    if [ "$RECENT_ERRORS" -gt 0 ]; then
        echo "   Recent errors:"
        grep -i "error\|exception\|traceback" "$LOG_DIR/tl_ego_stdout.log" 2>/dev/null | tail -5
    fi
else
    echo "   No stdout log found."
fi

echo ""
echo "=== Evaluation Complete ==="
echo "Next run: $(date -d '+1 day' -Iseconds 2>/dev/null || echo 'tomorrow')"