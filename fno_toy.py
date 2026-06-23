"""
TL-Ego FNO Toy — Step 2: Verify Fourier structure in concentration + competition trajectories

Approach:
1. Load 310+ ticks of resting trajectory
2. Extract concentration(t), competition(t), phase(t)
3. FFT analysis: what frequencies dominate?
4. Train a minimal Fourier layer predictor: 
   input = last 20 ticks of [conc, comp, noise_amp, diversity_n]
   output = next tick's [conc, comp, phase_change]
"""

import json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

# ── 1. Load data ──────────────────────────────────────────────────────────────
path = Path(__file__).parent / "data" / "resting_trajectory.json"
with open(path) as f:
    raw = json.load(f)

traj = raw.get("trajectory", raw)
print(f"Loaded {len(traj)} ticks")

# ── 2. Extract time series ────────────────────────────────────────────────────
ticks = []
cons = []
comps = []
noise_amps = []
diversities = []
phases = []
stagnations = []
n_activated = []

for t in traj:
    if not isinstance(t, dict):
        continue
    ticks.append(t.get("tick", len(ticks)))
    cons.append(t.get("concentration", 0))
    comps.append(t.get("competition", 0))
    noise_amps.append(t.get("noise_amp", 0))
    diversities.append(t.get("diversity_n", 0))
    phases.append(t.get("phase", "unknown"))
    stagnations.append(t.get("stagnation_streak", 0))
    n_activated.append(t.get("n_activated", 0))

cons = np.array(cons, dtype=np.float64)
comps = np.array(comps, dtype=np.float64)
noise_amps = np.array(noise_amps, dtype=np.float64)
diversities = np.array(diversities, dtype=np.float64)
stagnations = np.array(stagnations, dtype=np.float64)
arrays = {"concentration": cons, "competition": comps,
          "noise_amp": noise_amps, "diversity": diversities}

print(f"\nTrajectory range: tick {ticks[0]} to {ticks[-1]}")
print(f"Concentration: mean={cons.mean():.4f} std={cons.std():.4f}")
print(f"Competition:    mean={comps.mean():.4f} std={comps.std():.4f}")

# ── 3. FFT Analysis ──────────────────────────────────────────────────────────
fig, axes = plt.subplots(3, 2, figsize=(14, 10))
time_ax = axes[0, 1]
freq_ax = axes[0, 0]
corr_ax = axes[1, 0]
hist_ax = axes[1, 1]
phase_ax = axes[2, 0]
topo_ax = axes[2, 1]

# Time domain
time_ax.plot(ticks, cons, label="concentration", alpha=0.7)
time_ax.plot(ticks, comps, label="competition", alpha=0.7)
time_ax.set_xlabel("tick")
time_ax.set_ylabel("value")
time_ax.set_title("Time-domain trajectory")
time_ax.legend()
time_ax.grid(True, alpha=0.3)

# Frequency domain — FFT of concentration
n = len(cons)
fft_conc = np.fft.rfft(cons - cons.mean())
fft_comp = np.fft.rfft(comps - comps.mean())
freqs = np.fft.rfftfreq(n, d=1)[1:]  # skip DC

amp_conc = np.abs(fft_conc[1:])
amp_comp = np.abs(fft_comp[1:])

freq_ax.plot(freqs, amp_conc, label="|FFT(concentration)|", alpha=0.7)
freq_ax.plot(freqs, amp_comp, label="|FFT(competition)|", alpha=0.7)
freq_ax.set_xlabel("frequency (cycles/tick)")
freq_ax.set_ylabel("amplitude")
freq_ax.set_title("Fourier spectrum")
freq_ax.set_xscale("log")
freq_ax.set_yscale("log")
freq_ax.legend()
freq_ax.grid(True, alpha=0.3)

# Dominant frequencies
peak_idx_conc = np.argmax(amp_conc) + 1  # shift back for DC offset
peak_idx_comp = np.argmax(amp_comp) + 1
print(f"\nDominant frequency (conc): {freqs[peak_idx_conc-1]:.4f} cyc/tick")
print(f"Dominant frequency (comp): {freqs[peak_idx_comp-1]:.4f} cyc/tick")
print(f"Period (conc): {1/freqs[peak_idx_conc-1]:.1f} ticks")
print(f"Period (comp): {1/freqs[peak_idx_comp-1]:.1f} ticks")

# Top 5 frequencies
top5_c = np.argsort(amp_conc)[-5:][::-1]
top5_p = np.argsort(amp_comp)[-5:][::-1]
print("\nTop 5 concentration frequencies:")
for i in top5_c:
    f = freqs[i]
    print(f"  {f:.4f} cyc/tick → period {1/f:.1f} ticks  (amp={amp_conc[i]:.2f})")
print("Top 5 competition frequencies:")
for i in top5_p:
    f = freqs[i]
    print(f"  {f:.4f} cyc/tick → period {1/f:.1f} ticks  (amp={amp_comp[i]:.2f})")

# Spectral slope (1/f or white?)
slope_c = np.polyfit(np.log(freqs[1:50]), np.log(amp_conc[1:50]), 1)[0]
slope_p = np.polyfit(np.log(freqs[1:50]), np.log(amp_comp[1:50]), 1)[0]
print(f"\nSpectral slope (1-50 cyc, conc): {slope_c:.2f} (pink=-1, white=0, brown=-2)")
print(f"Spectral slope (1-50 cyc, comp): {slope_p:.2f}")

# Cross-correlation
corr = np.correlate(cons - cons.mean(), comps - comps.mean(), mode="full")
lags = np.arange(-len(cons) + 1, len(cons))
mid = len(corr) // 2
corr_ax.plot(lags[mid-50:mid+50], corr[mid-50:mid+50])
corr_ax.axvline(0, color="red", linestyle="--", alpha=0.5, label="zero lag")
corr_ax.set_xlabel("lag (ticks)")
corr_ax.set_ylabel("cross-correlation")
corr_ax.set_title("conc ↔ comp cross-correlation (center ±50)")
corr_ax.legend()
corr_ax.grid(True, alpha=0.3)

# Phase distribution
phase_names = sorted(set(phases))
phase_counts = [phases.count(p) for p in phase_names]
colors = plt.cm.tab10(np.linspace(0, 1, len(phase_names)))
phase_ax.bar(phase_names, phase_counts, color=colors)
phase_ax.set_title("Phase distribution")
phase_ax.set_ylabel("ticks")
for i, (p, c) in enumerate(zip(phase_names, phase_counts)):
    phase_ax.text(i, c + 1, str(c), ha="center", fontsize=9)

# Phase transition analysis
transitions = sum(1 for i in range(1, len(phases)) if phases[i] != phases[i-1])
print(f"\nPhase transitions: {transitions} in {len(phases)} ticks")
print(f"Phase stability: {(1 - transitions/len(phases)) * 100:.1f}%")

# State space trajectory (conc vs comp colored by tick)
sc = topo_ax.scatter(cons, comps, c=ticks, cmap="viridis", s=10, alpha=0.7)
topo_ax.set_xlabel("concentration")
topo_ax.set_ylabel("competition")
topo_ax.set_title("State space trajectory (color=tick)")
plt.colorbar(sc, ax=topo_ax, label="tick")

plt.tight_layout()
plt.savefig(Path(__file__).parent / "data" / "fno_toy_analysis.png", dpi=150)
print(f"\nAnalysis saved to fno_toy_analysis.png")

# ── 4. Toy Fourier Predictor ────────────────────────────────────────────────
# Input: seq_len window of [conc, comp, noise_amp, diversity]
# Target: next tick's [conc, comp]
# Fourier feature: average amplitude in 3 frequency bands (low/med/high)

SEQ_LEN = 20
N_BANDS = 3
HIDDEN = 32

def compute_fft_features(seq):
    """From a sequence of scalars, compute band-averaged FFT amplitudes."""
    n = len(seq)
    fft = np.fft.rfft(seq - seq.mean())
    amps = np.abs(fft[1:])  # skip DC
    if len(amps) < N_BANDS:
        amps = np.pad(amps, (0, N_BANDS - len(amps)))
    # Split into 3 bands
    split = np.array_split(amps, N_BANDS)
    return np.array([s.mean() if len(s) > 0 else 0 for s in split])

def build_dataset(series_list, seq_len=SEQ_LEN):
    """Build FNO-style features: each sample = FFT features + raw tail."""
    X, y_raw = [], []
    for i in range(seq_len, len(series_list[0])):
        feat_vec = []
        for s in series_list:
            window = s[i-seq_len:i]
            fft_feat = compute_fft_features(window)
            feat_vec.extend(fft_feat)
            feat_vec.extend([window[-1], window.min(), window.max(), window.std()])
        X.append(feat_vec)
        y_raw.append([
            series_list[0][i],  # target conc
            series_list[1][i],  # target comp
        ])
    return np.array(X, dtype=np.float64), np.array(y_raw, dtype=np.float64)

X, y = build_dataset([cons, comps, noise_amps, diversities])
print(f"\nToy dataset: X.shape={X.shape}, y.shape={y.shape}")

# Simple linear + Fourier predictor (no FFT, just band features)
# Weights = learned Fourier filter coefficients
split = int(len(X) * 0.8)
X_train, X_test = X[:split], X[split:]
y_train, y_test = y[:split], y[split:]

# Train: ridge regression (closed-form)
from numpy.linalg import lstsq
X_train_aug = np.c_[X_train, np.ones(X_train.shape[0])]
X_test_aug = np.c_[X_test, np.ones(X_test.shape[0])]

w_conc = lstsq(X_train_aug, y_train[:, 0], rcond=None)[0]
w_comp = lstsq(X_train_aug, y_train[:, 1], rcond=None)[0]

y_pred_conc = X_test_aug @ w_conc
y_pred_comp = X_test_aug @ w_comp

# Evaluate
mse_conc = np.mean((y_test[:, 0] - y_pred_conc) ** 2)
mse_comp = np.mean((y_test[:, 1] - y_pred_comp) ** 2)
var_conc = np.var(y_test[:, 0])
var_comp = np.var(y_test[:, 1])
r2_conc = 1 - mse_conc / var_conc
r2_comp = 1 - mse_comp / var_comp

print(f"\n─── Toy FNO Predictor Results ───")
print(f"Train: {X_train.shape[0]}, Test: {X_test.shape[0]}")
print(f"Concentration: MSE={mse_conc:.6f}, R2={r2_conc:.4f}")
print(f"Competition:    MSE={mse_comp:.6f}, R2={r2_comp:.4f}")
print(f"Baseline (predict mean): conc R2=0.0, comp R2=0.0")

# Persistence baseline (predict last value)
y_pred_persist_conc = X_test[:, 4]  # conc raw tail last value at feature idx 4
y_pred_persist_comp = X_test[:, 5]  # comp raw tail last value
mse_p_conc = np.mean((y_test[:, 0] - y_pred_persist_conc) ** 2)
mse_p_comp = np.mean((y_test[:, 1] - y_pred_persist_comp) ** 2)
print(f"\nPersistence baseline:")
print(f"Concentration: MSE={mse_p_conc:.6f}, R2={1 - mse_p_conc/var_conc:.4f}")
print(f"Competition:    MSE={mse_p_comp:.6f}, R2={1 - mse_p_comp/var_comp:.4f}")

# Prediction visualization
fig2, axes2 = plt.subplots(2, 1, figsize=(14, 8))
ax_c, ax_p = axes2
test_ticks = list(range(len(y_test)))
ax_c.plot(test_ticks, y_test[:, 0], label="true concentration", alpha=0.7)
ax_c.plot(test_ticks, y_pred_conc, label="FNO predictor", alpha=0.7, linestyle="--")
ax_c.legend()
ax_c.set_title(f"Concentration prediction (test set, R2={r2_conc:.4f})")
ax_c.grid(True, alpha=0.3)

ax_p.plot(test_ticks, y_test[:, 1], label="true competition", alpha=0.7)
ax_p.plot(test_ticks, y_pred_comp, label="FNO predictor", alpha=0.7, linestyle="--")
ax_p.legend()
ax_p.set_title(f"Competition prediction (test set, R2={r2_comp:.4f})")
ax_p.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig(Path(__file__).parent / "data" / "fno_toy_prediction.png", dpi=150)
print(f"Prediction plot saved to fno_toy_prediction.png")

# ── 5. FNO Benefit Estimate ─────────────────────────────────────────────────
print(f"\n─── FNO Benefit Assessment ───")
print(f"Fourier structure detected: YES" if np.any(amp_conc > 3*amp_conc.std()) else "Fourier structure detected: NO")
print(f"Predictability (conc R2): {r2_conc:.4f}")
print(f"Predictability (comp R2): {r2_comp:.4f}")

# Phase transition prediction accuracy
phase_numeric = np.array([0 if p == 'explore' else 1 if p == 'consolidate' else 2 if p == 'burst' else 3 for p in phases])
phase_changes = np.diff(phase_numeric)
print(f"Phase transition frequency: {np.sum(phase_changes != 0)} / {len(phase_changes)} ticks")

# The key question: can we predict phase changes from FFT features?
# If conc+comp oscillations have systematic phase shifts before transitions → FNO can learn them
print(f"\nTo integrate into TL-Ego:")
print(f"1. Predict next N ticks of [conc, comp, dominant_type]")
print(f"2. Use prediction to pre-empt stagnation (inject noise BEFORE stuck)")
print(f"3. Use prediction to guide S2 towards attractor basin boundaries")
