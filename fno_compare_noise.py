"""
FNO Compare: pre-pink-noise vs post-pink-noise spectra

1. Load trajectory_log.jsonl (accumulated tick data)
2. Split at tick reset boundaries
3. Compare FFT spectra between white-dominant and pink-dominant runs
4. Report phase stability change
"""

import json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
LOG_FILE = DATA_DIR / "trajectory_log.jsonl"

# ── 1. Load and split runs ───────────────────────────────────────────────────
runs = {}  # run_id -> {ticks: [], cons: [], comps: [], phases: [], mode_hints: []}
with open(LOG_FILE) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        tick = entry.get("tick", 0)
        run_id = entry.get("run_id", "default")

        if run_id not in runs:
            runs[run_id] = {
                "ticks": [], "cons": [], "comps": [],
                "phases": [], "noise_modes": [],
                "noise_amps": [], "diversities": []
            }
        r = runs[run_id]
        r["ticks"].append(tick)
        r["cons"].append(entry.get("concentration", 0))
        r["comps"].append(entry.get("competition", 0))
        r["phases"].append(entry.get("phase", "unknown"))
        r["noise_modes"].append(entry.get("noise_mode", "unknown"))
        r["noise_amps"].append(entry.get("noise_amp", 0))
        r["diversities"].append(entry.get("diversity_n", 0))

# If no run_id in data (older format), try detecting resets
if "default" in runs and len(runs) == 1:
    # Detect tick resets (tick goes backwards)
    ticks = runs["default"]["ticks"]
    segments = []
    seg_start = 0
    for i in range(1, len(ticks)):
        if ticks[i] < ticks[i-1]:  # reset detected
            segments.append(("run", seg_start, i))
            seg_start = i
    segments.append(("run_latest", seg_start, len(ticks)))

    # Reorganize into separate runs
    runs = {}
    for name, start, end in segments:
        r = runs[name] = {
            "ticks": ticks[start:end],
            "cons": runs["default"]["cons"][start:end],
            "comps": runs["default"]["comps"][start:end],
            "phases": runs["default"]["phases"][start:end],
            "noise_modes": runs["default"]["noise_modes"][start:end],
            "noise_amps": runs["default"]["noise_amps"][start:end],
            "diversities": runs["default"]["diversities"][start:end],
        }
    del runs["default"]

print(f"Found {len(runs)} run segments")
for name, r in runs.items():
    n_ticks = len(r["ticks"])
    tick_range = f"{r['ticks'][0]}-{r['ticks'][-1]}"
    # Count noise modes
    modes = {}
    for m in r["noise_modes"]:
        modes[m] = modes.get(m, 0) + 1
    mode_str = ", ".join(f"{m}={c}" for m, c in sorted(modes.items()))
    phases = {}
    for p in r["phases"]:
        phases[p] = phases.get(p, 0) + 1
    phase_changes = sum(1 for i in range(1, len(r["phases"])) if r["phases"][i] != r["phases"][i-1])
    stability = (1 - phase_changes / len(r["phases"])) * 100 if len(r["phases"]) > 1 else 100
    
    cons = np.array(r["cons"])
    comps = np.array(r["comps"])
    print(f"\n  {name}: {n_ticks} ticks ({tick_range})")
    print(f"    Noise modes: {mode_str}")
    print(f"    Phase stability: {stability:.1f}% ({phase_changes} transitions in {n_ticks} ticks)")
    print(f"    Mean conc={cons.mean():.3f} comp={comps.mean():.3f}")
    
    # FFT if enough data
    if n_ticks >= 10:
        n = n_ticks
        fft_c = np.fft.rfft(cons - cons.mean())
        fft_p = np.fft.rfft(comps - comps.mean())
        freqs = np.fft.rfftfreq(n, d=1)[1:]  # skip DC
        amp_c = np.abs(fft_c[1:])
        amp_p = np.abs(fft_p[1:])
        
        if len(freqs) > 0:
            peak_idx = np.argmax(amp_c) if len(amp_c) > 0 else 0
            peak_freq = freqs[peak_idx] if peak_idx < len(freqs) else 0
            print(f"    Dominant freq: {peak_freq:.3f} cyc/tick (period {1/peak_freq:.1f} ticks)" if peak_freq > 0 else "    No dominant freq")
            
            # Spectral slope (1/f estimate) - use first 25% of frequencies
            n_slope = max(5, min(50, len(freqs) // 4))
            if len(freqs) >= n_slope and np.all(freqs[:n_slope] > 0) and np.all(amp_c[:n_slope] > 0):
                slope_c = np.polyfit(np.log(freqs[:n_slope]), np.log(amp_c[:n_slope]), 1)[0]
                slope_p = np.polyfit(np.log(freqs[:n_slope]), np.log(amp_p[:n_slope]), 1)[0]
                print(f"    Spectral slope: conc={slope_c:.2f} comp={slope_p:.2f} (pink=-1, white=0)")
    
    # Phase transition pattern
    transition_intervals = []
    last_transition = 0
    for i in range(1, len(r["phases"])):
        if r["phases"][i] != r["phases"][i-1]:
            transition_intervals.append(i - last_transition)
            last_transition = i
    if transition_intervals:
        print(f"    Mean dwell time: {np.mean(transition_intervals):.1f} ticks between phase changes")

# ── 2. Cross-run comparison plot ─────────────────────────────────────────────
fig, axes = plt.subplots(3, len(runs), figsize=(6*len(runs), 12))
if len(runs) == 1:
    axes = axes.reshape(3, 1)

for i, (name, r) in enumerate(runs.items()):
    if len(r["ticks"]) < 5:
        continue
    
    cons = np.array(r["cons"])
    comps = np.array(r["comps"])
    
    # Time series
    ax = axes[0, i]
    ax.plot(r["ticks"], cons, label="concentration", alpha=0.7, color="#2196F3")
    ax2 = ax.twinx()
    ax2.plot(r["ticks"], comps, label="competition", alpha=0.7, color="#FF5722")
    ax.set_title(f"{name}\n({len(r['ticks'])} ticks)")
    ax.set_xlabel("tick")
    ax.legend(loc="upper left")
    ax2.legend(loc="upper right")
    ax.grid(True, alpha=0.3)
    
    # Phase transitions
    ax = axes[1, i]
    phase_colors = {"explore": "#4CAF50", "consolidate": "#FF9800", "burst": "#F44336"}
    for t_idx, p in enumerate(r["phases"]):
        color = phase_colors.get(p, "#9E9E9E")
        ax.bar(r["ticks"][t_idx], 1, width=0.8, color=color, alpha=0.6)
    ax.set_title(f"Phase (green=explore, orange=consolidate, red=burst)")
    ax.set_ylim(0, 2)
    ax.set_xlim(min(r["ticks"]), max(r["ticks"]))
    ax.set_yticks([])
    
    # FFT spectrum
    if len(r["ticks"]) >= 10:
        ax = axes[2, i]
        n = len(r["ticks"])
        fft_c = np.fft.rfft(cons - cons.mean())
        freqs = np.fft.rfftfreq(n, d=1)[1:]
        amp_c = np.abs(fft_c[1:])
        
        ax.plot(freqs, amp_c, color="#2196F3", alpha=0.7)
        ax.set_title(f"FFT spectrum")
        ax.set_xlabel("frequency (cyc/tick)")
        ax.set_ylabel("amplitude")
        ax.grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig(DATA_DIR / "fno_compare_noise.png", dpi=150)
print(f"\nComparison plot saved to fno_compare_noise.png")

# ── 3. Summary verdict ──────────────────────────────────────────────────────
print(f"\n─── Summary ───")
if len(runs) >= 2:
    names = list(runs.keys())
    r1, r2 = runs[names[0]], runs[names[1]]
    sm1 = (1 - sum(1 for i in range(1, len(r1["phases"])) if r1["phases"][i] != r1["phases"][i-1]) / len(r1["phases"])) * 100
    sm2 = (1 - sum(1 for i in range(1, len(r2["phases"])) if r2["phases"][i] != r2["phases"][i-1]) / len(r2["phases"])) * 100
    print(f"Phase stability delta: {sm2:.1f}% - {sm1:.1f}% = {sm2-sm1:+.1f}%")
