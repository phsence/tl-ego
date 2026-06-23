"""Check FNO integration state and update _fno_predict for new FNO API."""
import re

with open(r'projects/tl-ego/tl_ego_server.py', encoding='utf-8') as f:
    raw = f.read()

clean = re.sub(r'\x1b\[[0-9;]*m', '', raw)

# Find the _fno_predict function
fn_start = clean.find('def _fno_predict(')
if fn_start < 0:
    print("ERROR: _fno_predict not found")
    exit(1)

# Show the function
fn_end = clean.find('\ndef _fno_choose', fn_start)
print("=== Current _fno_predict function ===")
print(clean[fn_start:fn_end])

# Also show _fno_choose_noise
fn_start2 = clean.find('def _fno_choose_noise', fn_end - 50)
fn_end2 = clean.find('\ndef resting_tick(', fn_start2)
print("\n=== Current _fno_choose_noise function ===")
print(clean[fn_start2:fn_end2])

# Show the prediction call in resting_tick
call_site = clean.find('FNO: run prediction')
if call_site >= 0:
    end = clean.find('\n\n', call_site + 400)
    print("\n=== FNO prediction call in resting_tick ===")
    print(clean[call_site:end])
