#!/usr/bin/env python3
"""
fno_noise_adaptor.py — Fourier Neural Operator for TL-Ego Noise Control

取代固定的 _rotate_noise_mode()，用 FNO 预演各噪声模式对后续 5 tick 的影响，
选最优噪声。在 30s tick 间隔内完成推理。

Architecture:
  Input:  W=20 tick 窗口 × 20 维特征
  Body:   Fourier Layer × 3 (hidden=64, modes=16)
  Head:   MLP → 4 条分支 (white/pink/sparse/none)
          每条分支预测 N=5 tick 后的: dominant, conc, competition, stag, diversity

Training:
  现有 tick 数据 → 80% train / 20% val
  Loss: MSE(state_pred) + 0.1 × dominance_crossentropy

Integration:
  from fno_noise_adaptor import FNOAdaptor
  adaptor = FNOAdaptor()  # loads cached weights
  best_noise = adaptor.choose_noise(last_20_ticks)
  # → 替换 _rotate_noise_mode() 返回值

Context:
  TL-Ego Server (port 8766)
  resting_trajectory.jsonl — 22K+ 条历史 tick 数据
  PyTorch 2.11 + CUDA
"""

import json, os, sys, time, pickle
from pathlib import Path
from collections import OrderedDict

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

# ─── paths ─────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
JSONL_PATH = SCRIPT_DIR / "resting_trajectory.jsonl"
CACHE_PATH = SCRIPT_DIR / ".fno_cache.pkl"
WEIGHTS_PATH = SCRIPT_DIR / ".fno_weights.pt"

# ─── constants ─────────────────────────────────────────────────────────
DOMAINS = ["memory", "sensory", "action", "integrator", "decider"]
NOISES  = ["white", "pink", "sparse", "none"]
WINDOW  = 20        # how many past ticks to look at
HORIZON = 5         # how many future ticks to predict
HIDDEN  = 64
MODES   = 16        # Fourier modes
N_FEATURES = 16     # input dim (state only, no noise)
NOISE_DIM = 4       # one-hot for noise mode

# ─── feature map ───────────────────────────────────────────────────────
def tick_state_vec(tick: dict) -> np.ndarray:
    """Convert a tick dict into 16-dim state vector (no noise info)."""
    dom = tick.get("dominant", "memory")
    dom_onehot = [1.0 if dom == d else 0.0 for d in DOMAINS]

    conc   = tick.get("concentration", 0.0)
    compet = tick.get("competition", 0.0)
    stag   = tick.get("stagnation_streak", 0.0) / 10.0
    diversity = tick.get("diversity_n", 0.0) / 15.0
    burst = 1.0 if tick.get("burst", False) else 0.0

    type_counts = tick.get("type_counts", {})
    tc = [type_counts.get(d, 0) / 30.0 for d in DOMAINS]

    all_scalars = [conc, compet, stag, diversity, burst]
    used = [x for x in all_scalars if x is not None]
    # Pad to 5 if any missing
    while len(used) < 5:
        used.append(0.0)
    vec = np.array(dom_onehot + used + tc, dtype=np.float32)
    if vec.shape != (16,):
        print(f"[FNO] WARN: vec shape {vec.shape} from tick {tick.get('tick','?')}, padding")
        vec = np.pad(vec, (0, 16 - len(vec)), 'constant')[:16]
    return vec


def noise_mode_onehot(tick: dict) -> np.ndarray:
    """5-dim: 4 one-hot + 1 amp"""
    nm = tick.get("noise_mode", None)
    if nm is None:
        nm = "white"
    na = tick.get("noise_amp", None)
    if na is None:
        na = 0.15
    oh = [1.0 if nm == n else 0.0 for n in NOISES]
    return np.array(oh + [float(na)], dtype=np.float32)


# ─── data loader ──────────────────────────────────────────────────────
def load_tick_data(jsonl_path=None):
    """Read JSONL, deduplicate by tick (keep last), return sorted list of dicts."""
    if jsonl_path is None:
        jsonl_path = JSONL_PATH
    if not jsonl_path.exists():
        print(f"[FNO] Data file not found: {jsonl_path}")
        return []

    data = OrderedDict()
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                tick = json.loads(line)
                data[tick["tick"]] = tick
            except (json.JSONDecodeError, KeyError):
                continue

    result = list(data.values())
    # Check for missing keys
    missing_noise = [t["tick"] for t in result if "noise_mode" not in t]
    if missing_noise:
        print(f"[FNO] {len(missing_noise)} ticks missing noise_mode — filing with 'white'")
        for t in result:
            if "noise_mode" not in t:
                t["noise_mode"] = "white"
                t["noise_amp"] = 0.15

    result.sort(key=lambda x: x["tick"])
    print(f"[FNO] Loaded {len(result)} unique ticks ({len(data)} total entries deduplicated)")
    return result


def build_sequences(ticks, window=WINDOW, horizon=HORIZON):
    """Build (X, Y) pairs for training.

    X: (W, 16 state + 5 noise) = (W, 21) — conditioned noise to test
    Y: (H, 15) flattened = 75 — future state prediction

    CRITICAL: at train time, the noise appended to the LAST tick is the
    *candidate* noise — i.e. the noise that was actually used (from ticks[i]).
    This teaches: given state history + candidate noise, what future state?
    """
    n = len(ticks)
    xs, ys = [], []
    for i in range(window, n - horizon):
        # State from window
        state_window = np.stack([tick_state_vec(ticks[j]) for j in range(i - window, i)], axis=0)  # (W, 16)

        # Noise: only the LAST tick's noise matters (the one being evaluated)
        # But we broadcast it to every step so the FNO can learn conditioning
        noise = noise_mode_onehot(ticks[i])  # (5,) — the noise applied at tick[i]
        noise_broadcast = np.tile(noise, (window, 1))  # (W, 5)

        x = np.concatenate([state_window, noise_broadcast], axis=1)  # (W, 21)

        # Target: future state (H steps ahead)
        y_future = np.stack([tick_state_vec(ticks[i + h]) for h in range(horizon)], axis=0)  # (H, 16)
        y = y_future.flatten()  # 80-dim

        xs.append(x)
        ys.append(y)
    return np.array(xs, dtype=np.float32), np.array(ys, dtype=np.float32)


# ─── Fourier Layer ────────────────────────────────────────────────────
class FourierLayer(nn.Module):
    """Spectral convolution along the time dimension.

    Uses real-valued weights on real+imag concatenated Fourier features,
    then recombines. Avoids complex tensor dtype issues.
    """
    def __init__(self, hidden, modes):
        super().__init__()
        self.hidden = hidden
        self.modes = modes
        scale = 1.0 / (hidden * modes)
        self.W = nn.Parameter(scale * torch.randn(modes, hidden, hidden))
        self.b = nn.Parameter(torch.zeros(1, hidden))

    def forward(self, x):
        # x: (B, W, hidden)
        B, W, H = x.shape

        # Real FFT
        x_fft = torch.fft.rfft(x, dim=1)  # (B, W//2+1, hidden) complex
        n_modes = min(self.modes, x_fft.shape[1])

        # Split real/imag and project
        real = x_fft[:, :n_modes, :].real  # (B, n_modes, hidden)
        imag = x_fft[:, :n_modes, :].imag  # (B, n_modes, hidden)

        # Spectral filtering: matmul each mode
        out_real = torch.zeros_like(real)
        out_imag = torch.zeros_like(imag)
        for i in range(n_modes):
            Wi = self.W[i]                                 # (hidden, hidden)
            r_i = real[:, i, :]                             # (B, hidden)
            im_i = imag[:, i, :]                            # (B, hidden)
            out_real[:, i, :] = torch.matmul(r_i, Wi)
            out_imag[:, i, :] = torch.matmul(im_i, Wi)

        # Recombine into complex
        out_fft = torch.complex(out_real, out_imag)         # (B, n_modes, hidden)
        # Pad back to full FFT size
        x_fft_out = torch.zeros_like(x_fft)
        x_fft_out[:, :n_modes, :] = out_fft

        x_spec = torch.fft.irfft(x_fft_out, n=W, dim=1)    # (B, W, hidden)
        return x_spec + self.b


class FNO1D(nn.Module):
    """FNO for 1D temporal sequences.

    Input:  (B, W, 21)   — 16 state + 5 noise (noise broadcast across window)
    Output: (B, 80)      — H×16 flattened future state
    """
    def __init__(self, in_dim=21, hidden=HIDDEN, modes=MODES, out_dim=80):
        super().__init__()
        self.fc0 = nn.Linear(in_dim, hidden)
        self.layers = nn.ModuleList([
            FourierLayer(hidden, modes) for _ in range(3)
        ])
        self.fc1 = nn.Linear(hidden, 64)
        self.fc2 = nn.Linear(64, out_dim)

    def forward(self, x, noise_override=None):
        # x: (B, W, 21)  — last 5 dims are noise embedding
        # noise_override: (B, 5) — replace the noise conditioning
        if noise_override is not None:
            # Replace last 5 dims with override noise (broadcast across W)
            B, W, _ = x.shape
            state = x[:, :, :-5]  # (B, W, 16)
            override = noise_override.unsqueeze(1).expand(-1, W, -1)  # (B, W, 5)
            x = torch.cat([state, override], dim=2)

        x = self.fc0(x)                          # (B, W, hidden)
        for layer in self.layers:
            x = layer(x)                         # (B, W, hidden)
        x = x.mean(dim=1)                        # (B, hidden) — pool over window
        x = F.relu(self.fc1(x))
        x = self.fc2(x)                          # (B, 80)
        return x


# ─── loss ─────────────────────────────────────────────────────────────
def fno_loss(pred, target):
    """pred, target: (B, 80)

    80 = HORIZON × 16 = 5 × 16 = future state flattened
    """
    state_pred = pred.reshape(-1, HORIZON, 16)   # (B, H, 16)
    state_targ = target.reshape(-1, HORIZON, 16) # (B, H, 16)

    # MSE on all 16 dims
    mse = F.mse_loss(state_pred, state_targ)

    # Domain prediction cross-entropy (first 5 dims = dominant one-hot for last horizon step)
    dom_pred = state_pred[:, -1, :5]  # (B, 5)
    dom_targ = state_targ[:, -1, :5]  # (B, 5)
    ce = F.cross_entropy(dom_pred, dom_targ.argmax(dim=1))

    return mse + 0.05 * ce


# ─── training ─────────────────────────────────────────────────────────
def train(save=True):
    ticks = load_tick_data()
    if len(ticks) < WINDOW + HORIZON + 10:
        print(f"[FNO] Not enough ticks ({len(ticks)}). Need at least {WINDOW + HORIZON + 10}")
        return None

    X, Y = build_sequences(ticks)
    print(f"[FNO] Sequences: {X.shape[0]} (W={WINDOW}, H={HORIZON})")

    # Split
    n = X.shape[0]
    split = int(n * 0.8)
    X_train, Y_train = X[:split], Y[:split]
    X_val, Y_val = X[split:], Y[split:]

    # Convert to tensor
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    Xt = torch.from_numpy(X_train).to(device)
    Yt = torch.from_numpy(Y_train).to(device)
    Xv = torch.from_numpy(X_val).to(device)
    Yv = torch.from_numpy(Y_val).to(device)

    model = FNO1D().to(device)
    optim = torch.optim.AdamW(model.parameters(), lr=5e-4, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optim, T_max=100)

    batch_size = 64
    best_val = float("inf")

    print(f"[FNO] Training on {device} — {X_train.shape[0]} train / {X_val.shape[0]} val")
    for epoch in range(200):
        model.train()
        perm = torch.randperm(Xt.shape[0])
        loss_sum = 0.0
        for i in range(0, Xt.shape[0], batch_size):
            idx = perm[i:i + batch_size]
            pred = model(Xt[idx])
            loss = fno_loss(pred, Yt[idx])
            optim.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optim.step()
            loss_sum += loss.item()

        # Validation
        model.eval()
        with torch.no_grad():
            pred_v = model(Xv)
            val_loss = fno_loss(pred_v, Yv).item()

        scheduler.step()

        if epoch % 20 == 0 or val_loss < best_val or epoch == 199:
            print(f"  epoch {epoch:3d} | train loss {loss_sum/(Xt.shape[0]//batch_size+1):.4f} | val loss {val_loss:.4f}")
            if val_loss < best_val:
                best_val = val_loss
                # Save best weights
                if save:
                    torch.save(model.state_dict(), WEIGHTS_PATH)

    print(f"[FNO] Training complete. Best val loss: {best_val:.4f}")
    return model


# ─── inference ────────────────────────────────────────────────────────
class FNOAdaptor:
    """Conditional FNO: run 4 forward passes (one per noise mode) and pick best."""

    def __init__(self, weights_path=None):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model = FNO1D().to(self.device)
        self.weights_path = weights_path or WEIGHTS_PATH
        self._loaded = False
        self._state_buffer = None  # rolling window: list of 16-dim state vecs
        self._last_noise = "pink"

    def load(self):
        if self.weights_path.exists():
            self.model.load_state_dict(torch.load(self.weights_path, map_location=self.device, weights_only=True))
            self.model.eval()
            self._loaded = True
            print(f"[FNO] Loaded weights from {self.weights_path}")
        else:
            print(f"[FNO] No weights at {self.weights_path}. Will train on first use.")
            self._loaded = False
        return self._loaded

    def push_tick(self, tick_dict):
        """Feed a new tick into the rolling state buffer (noise-free)."""
        vec = tick_state_vec(tick_dict)
        if self._state_buffer is None:
            self._state_buffer = [vec] * WINDOW
        else:
            self._state_buffer.append(vec)
            if len(self._state_buffer) > WINDOW:
                self._state_buffer = self._state_buffer[-WINDOW:]

    def _noise_to_override(self, noise_name):
        """Convert noise name to (5,) override vector."""
        oh = [1.0 if noise_name == n else 0.0 for n in NOISES]
        return np.array(oh + [0.5], dtype=np.float32)  # default amp=0.5

    def choose_noise(self, tick_dict=None, return_scores=False):
        """
        Run FNO 4 times with different noise conditioning, pick what gives
        best predicted future state.

        Scoring formula:
          +3× diversity      (exploration)
          +2× (1-stagnation) (avoid deadlock)
          +1× competition    (healthy competition among attractors)
          -0.5× conc         (don't lock in too tightly)
        """
        if tick_dict is not None:
            self.push_tick(tick_dict)

        if self._state_buffer is None or len(self._state_buffer) < WINDOW:
            return "pink"

        if not self._loaded:
            return "pink"

        # Base input: (W, 16) state only
        state_win = np.stack(self._state_buffer[-WINDOW:], axis=0)  # (W, 16)

        scores = {}
        for noise in NOISES:
            # Build noise override broadcast
            override = self._noise_to_override(noise)  # (5,)
            noise_bcast = np.tile(override, (WINDOW, 1))  # (W, 5)
            x = np.concatenate([state_win, noise_bcast], axis=1)  # (W, 21)
            x_t = torch.from_numpy(x).unsqueeze(0).to(self.device)  # (1, W, 21)

            with torch.no_grad():
                pred = self.model(x_t)  # (1, 80)
                state = pred[0].reshape(HORIZON, 16).cpu().numpy()  # (H, 16)

            # state[i] = [dom_5, conc, compet, stag, diversity, burst, tc_5]
            last = state[-1]  # furthest horizon
            conc      = last[5]
            compet    = last[6]
            stag      = last[7]
            diversity = last[8]

            scor = (
                diversity * 3.0
                + (1.0 - stag) * 2.0
                + compet * 1.0
                - conc * 0.5
            )
            scores[noise] = float(scor)

        best = max(scores, key=scores.get)
        self._last_noise = best

        if return_scores:
            return best, scores
        return best


# ─── standalone CLI ───────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="FNO Noise Adaptor for TL-Ego")
    parser.add_argument("--train", action="store_true", help="Train FNO from historical data")
    parser.add_argument("--eval", action="store_true", help="Evaluate on validation data")
    parser.add_argument("--infer", type=int, nargs="?", const=-1, metavar="TICK_ID",
                        help="Infer best noise for last data (or --infer <tick_id>)")
    args = parser.parse_args()

    if args.train:
        model = train()
        if model is not None and args.eval:
            # Quick eval: print predictions for first val batch
            ticks = load_tick_data()
            X, Y = build_sequences(ticks)
            n = X.shape[0]
            split = int(n * 0.8)
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            Xv = torch.from_numpy(X[split:split+5]).to(device)
            model.eval()
            with torch.no_grad():
                pred = model(Xv)
            print(f"[FNO] Validation predictions: {pred.cpu().numpy()[0, -4:]} (noise scores)")

    elif args.infer:
        adaptor = FNOAdaptor()
        loaded = adaptor.load()
        if not loaded:
            print("[FNO] No trained weights. Run with --train first.")
            return

        ticks = load_tick_data()
        if args.infer == -1:
            target_ticks = ticks[-WINDOW:]
        else:
            target_ticks = [t for t in ticks if t["tick"] == args.infer]
            if not target_ticks:
                print(f"[FNO] Tick {args.infer} not found. Using last {WINDOW} ticks.")
                target_ticks = ticks[-WINDOW:]

        for t in target_ticks:
            adaptor.push_tick(t)

        best, scores = adaptor.choose_noise(return_scores=True)
        print(f"[FNO] Recommended noise: {best}")
        print(f"[FNO] Scores: {scores}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
