#!/usr/bin/env python3
"""
FNO (Fourier Neural Operator) for TL-Ego resting state trajectory.

What it learns:
    Given 20 tick window of 12-dim state vectors, predict next 10 ticks.
    4 scenario heads produce differentiated forecasts for pink/white/sparse/none.

    12-dim state: [concentration, competition, n_activated, noise_amp,
                   stagnation_streak, diversity_n, burst_flag,
                   dominant_onehot × 5]

Architecture (compact for 100K neuron system):
    Lift (12→32) → 4 Fourier layers (modes=16, width=32) → scenario_decoder
    → 4 heads → project to 12 each
    Output: sigmoid for [0,1] fields, softmax for dominant one-hot
    No explicit noise conditioning — noise_amp in state features is sufficient.

Usage:
    python fno_train.py --train trajectory.jsonl --epochs 300

Author: Lobster AI (2026-06-10)
"""

import os, sys, json, time, argparse
import numpy as np
from pathlib import Path

try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import Dataset, DataLoader
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    print("[fno] WARNING: torch not available.", flush=True)

# ─── Constants ───────────────────────────────
WINDOW_SIZE = 20
HORIZON = 10
N_SCALAR = 7    # conc, comp, n_activated, noise_amp, stagnation_streak, diversity_n, burst
N_DOMINANT = 8  # sensory, integrator, decider, action, memory, doubt, veto, rule_modification
FEATURE_DIM = N_SCALAR + N_DOMINANT  # 15

NOISE_MODES = ["pink", "white", "sparse", "none"]
N_NOISE_MODES = len(NOISE_MODES)

TYPE_NAMES = ["sensory", "integrator", "decider", "action", "memory",
              "doubt", "veto", "rule_modification"]


# ─── Data utilities ──────────────────────────

def dominant_onehot(dom_str):
    vec = [0.0] * N_DOMINANT
    try:
        vec[TYPE_NAMES.index(dom_str)] = 1.0
    except ValueError:
        pass
    return vec


def parse_entry(e):
    scalar = [
        float(e.get("concentration", 0)),
        float(e.get("competition", 0)),
        float(e.get("n_activated", 0)),
        float(e.get("noise_amp", 0)),
        float(e.get("stagnation_streak", 0)),
        float(e.get("diversity_n", 0)),
        float(1 if e.get("burst", False) else 0),
    ]
    dom_oh = dominant_onehot(e.get("dominant", "unknown"))
    return np.array(scalar + dom_oh, dtype=np.float32)


def trajectory_to_samples(trajectory, window=WINDOW_SIZE, horizon=HORIZON):
    """Sliding window: 20 → 10. Returns list of {X, Y, noise_mask}."""
    samples = []
    parsed = [parse_entry(e) for e in trajectory]

    for i in range(len(parsed) - window - horizon + 1):
        X = np.stack(parsed[i:i + window], axis=0)          # (20, 12)
        Y = np.stack(parsed[i + window:i + window + horizon], axis=0)  # (10, 12)

        # noise_mask: which mode was dominant during the target horizon
        target_noises = [
            e.get("noise_mode", "none")
            for e in trajectory[i + window:i + window + horizon]
        ]
        noise_mask = np.zeros((horizon, N_NOISE_MODES), dtype=np.float32)
        for t, nm in enumerate(target_noises):
            try:
                noise_mask[t, NOISE_MODES.index(nm)] = 1.0
            except ValueError:
                pass

        samples.append({"X": X, "Y": Y, "noise_mask": noise_mask,
                        "tick": trajectory[i + window - 1].get("tick", 0)})

    return samples


def load_trajectory(path, max_entries=1000):
    with open(path, "r", encoding="utf-8") as f:
        entries = [json.loads(line.strip()) for line in f if line.strip()]
    return entries[-max_entries:]


class TrajectoryDataset(Dataset):
    def __init__(self, samples):
        self.samples = samples
    def __len__(self):
        return len(self.samples)
    def __getitem__(self, idx):
        s = self.samples[idx]
        return {
            "X": torch.from_numpy(s["X"]),
            "Y": torch.from_numpy(s["Y"]),
            "noise_mask": torch.from_numpy(s["noise_mask"]),
            "tick": s["tick"],
        }


# ─── Fourier Layer ───────────────────────────

class FourierLayer(nn.Module):
    """FFT → linear in freq domain → iFFT + MLP + skip."""

    def __init__(self, width, modes):
        super().__init__()
        self.width = width
        self.modes = modes
        self.weights = nn.Parameter(torch.randn(modes, width, width, 2) * 0.02)
        self.mlp = nn.Sequential(
            nn.Linear(width, width * 2),
            nn.GELU(),
            nn.Linear(width * 2, width),
        )
        self.norm = nn.LayerNorm(width)

    def forward(self, x):
        """x: (batch, seq, width)"""
        batch, seq, _ = x.shape
        x_fft = torch.fft.rfft(x, dim=1)
        k = min(self.modes, x_fft.shape[1])

        # Extract real/imag from FFT output
        h = x_fft[:, :k, :]  # (b, k, w) complex
        h_real = h.real.unsqueeze(-1)  # (b, k, w, 1)
        h_imag = h.imag.unsqueeze(-1)

        wr = self.weights[:k, :, :, 0].unsqueeze(0)  # (1, k, w, w)
        wi = self.weights[:k, :, :, 1].unsqueeze(0)

        # (h_real + i*h_imag) * (wr + i*wi) = (h_real*wr - h_imag*wi) + i*(h_real*wi + h_imag*wr)
        out_real = (h_real * wr - h_imag * wi).sum(dim=2)
        out_imag = (h_real * wi + h_imag * wr).sum(dim=2)

        freq_out = torch.complex(out_real, out_imag)

        x_fft_out = torch.zeros_like(x_fft)
        x_fft_out[:, :k, :] = freq_out
        x = torch.fft.irfft(x_fft_out, n=seq, dim=1)

        x = self.norm(x + self.mlp(x))
        return x


# ─── FNO with 4-scenario heads ───────────────

class FNO(nn.Module):
    """FNO with 4 scenario heads for noise comparison.

    Shared encoder → 4 heads → 4 forecasts.
    No explicit noise conditioning — noise_amp in features is enough.
    """

    def __init__(self, input_dim=FEATURE_DIM, output_dim=FEATURE_DIM,
                 width=32, modes=16, n_layers=4,
                 window=WINDOW_SIZE, horizon=HORIZON):
        super().__init__()
        self.window = window
        self.horizon = horizon
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.width = width

        self.lift = nn.Linear(input_dim, width)
        self.pos_enc = nn.Parameter(torch.randn(1, window + horizon, width) * 0.02)

        self.fourier_layers = nn.ModuleList([
            FourierLayer(width, modes) for _ in range(n_layers)
        ])

        # Shared decoder
        self.decoder = nn.Sequential(
            nn.Linear(width, width * 2),
            nn.GELU(),
            nn.Linear(width * 2, width),
        )

        # 4 scenario heads
        self.scenario_heads = nn.ModuleList([
            nn.Linear(width, output_dim) for _ in range(N_NOISE_MODES)
        ])

        self._init_weights()

    def _init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight, gain=0.5)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    def _clamp_output(self, raw):
        """Apply domain-specific clamps to output."""
        out = raw.clone()
        out[..., 0] = torch.sigmoid(raw[..., 0])      # conc in [0,1]
        out[..., 1] = torch.sigmoid(raw[..., 1])      # comp in [0,1]
        out[..., 6] = torch.sigmoid(raw[..., 6])      # burst in [0,1]
        out[..., 2:6] = torch.relu(raw[..., 2:6])     # non-negative scalars
        # Dominant one-hot → softmax
        raw_dom = raw[..., 7:]
        dom_sum = torch.relu(raw_dom).sum(dim=-1, keepdim=True) + 1e-8
        out[..., 7:] = torch.relu(raw_dom) / dom_sum
        return out

    def _encode(self, x):
        """Shared encoder: lift → pos → Fourier layers."""
        h = self.lift(x)
        h = h + self.pos_enc[:, :self.window, :]
        for layer in self.fourier_layers:
            h = layer(h)

        # Pool to horizon
        h_pooled = h.mean(dim=1, keepdim=True)
        h_out = h_pooled.expand(-1, self.horizon, -1)
        h_out = h_out + self.pos_enc[:, self.window:, :]
        return self.decoder(h_out)

    def forward(self, x, noise_mask=None):
        """Forward pass with optional noise mask supervision.

        x: (batch, window, 12)
        noise_mask: optional (batch, horizon, 4) — one-hot noise mode per target tick

        Training: returns (batch, horizon, 12) — reconstruction
        If noise_mask provided, uses weighted average of 4 scenario heads.
        """
        h = self._encode(x)  # (batch, horizon, width)
        outputs = []
        for i in range(N_NOISE_MODES):
            raw = self.scenario_heads[i](h)  # (batch, horizon, 12)
            outputs.append(self._clamp_output(raw))

        if noise_mask is not None:
            # noise_mask: (batch, horizon, 4) — one-hot or weighted
            out = torch.zeros_like(outputs[0])
            for i in range(N_NOISE_MODES):
                out = out + outputs[i] * noise_mask[:, :, i:i+1]
            return out
        else:
            return sum(outputs) / N_NOISE_MODES

    def predict_scenarios(self, x):
        """Run all 4 scenario heads for comparison.

        x: (1, window, 12)
        Returns: dict {noise_mode: (horizon, 12)}
        """
        h = self._encode(x)
        results = {}
        for i, nm in enumerate(NOISE_MODES):
            raw = self.scenario_heads[i](h)
            results[nm] = self._clamp_output(raw)[0].detach().cpu().numpy()
        return results


# ─── Training ─────────────────────────────────

def train_fno(samples, epochs=300, batch_size=16, lr=1e-3, device="cpu"):
    dataset = TrajectoryDataset(samples)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    model = FNO().to(device)
    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-5)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    loss_fn = nn.MSELoss()

    loss_history = []
    best_loss = float("inf")

    print(f"[fno] Training: {len(samples)} samples, {epochs} epochs, "
          f"batch={batch_size}, device={device}", flush=True)

    for epoch in range(epochs):
        epoch_loss = 0.0
        n_batches = 0

        for batch in loader:
            X = batch["X"].to(device)
            Y = batch["Y"].to(device)
            noise_mask = batch["noise_mask"].to(device)

            pred = model(X, noise_mask)
            loss = loss_fn(pred, Y)

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()

            epoch_loss += loss.item()
            n_batches += 1

        avg_loss = epoch_loss / max(n_batches, 1)
        loss_history.append(avg_loss)
        scheduler.step()

        if (epoch + 1) % 30 == 0 or epoch == 0:
            print(f"  ep {epoch+1:3d}/{epochs} loss={avg_loss:.6f} "
                  f"lr={scheduler.get_last_lr()[0]:.2e}", flush=True)

        if avg_loss < best_loss:
            best_loss = avg_loss

    print(f"[fno] Done. Best loss: {best_loss:.6f}", flush=True)
    return model, loss_history


def save_model(model, path, loss_history=None, extra=None):
    config = {
        "input_dim": model.input_dim,
        "output_dim": model.output_dim,
        "width": model.width,
        "window": model.window,
        "horizon": model.horizon,
        "n_fourier_layers": len(model.fourier_layers),
        "modes": model.fourier_layers[0].modes,
    }
    save_dict = {
        "model_state": model.state_dict(),
        "config": config,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if loss_history:
        save_dict["loss_history"] = loss_history[-100:]
    if extra:
        save_dict.update(extra)
    torch.save(save_dict, path)
    print(f"[fno] Saved to {path}", flush=True)


def load_model(path, device="cpu"):
    ckpt = torch.load(path, map_location=device, weights_only=False)
    cfg = ckpt["config"]
    model = FNO(
        input_dim=cfg.get("input_dim", FEATURE_DIM),
        output_dim=cfg.get("output_dim", FEATURE_DIM),
        width=cfg.get("width", 32),
        modes=cfg.get("modes", 16),
        n_layers=cfg.get("n_fourier_layers", 4),
        window=cfg.get("window", WINDOW_SIZE),
        horizon=cfg.get("horizon", HORIZON),
    ).to(device)
    model.load_state_dict(ckpt["model_state"])
    model.eval()
    print(f"[fno] Loaded from {path} ({ckpt.get('timestamp','?')})", flush=True)
    return model, ckpt


def evaluate(model, samples, device="cpu"):
    model.eval()
    scalar_errors = np.zeros(N_SCALAR)
    dom_errors = 0
    n = 0

    with torch.no_grad():
        for s in samples:
            X = torch.from_numpy(s["X"]).unsqueeze(0).to(device)
            Y = s["Y"]
            nm = torch.from_numpy(s["noise_mask"]).unsqueeze(0).to(device)
            pred = model(X, nm)[0].cpu().numpy()

            scalar_errors += np.mean(np.abs(pred[:, :N_SCALAR] - Y[:, :N_SCALAR]), axis=0)
            pd = np.argmax(pred[:, N_SCALAR:], axis=1)
            td = np.argmax(Y[:, N_SCALAR:], axis=1)
            dom_errors += np.mean(pd != td)
            n += 1

    scalar_mae = scalar_errors / max(n, 1)
    result = {
        "n": n,
        "scalar_mae": scalar_mae.tolist(),
        "scalar_names": ["concentration", "competition", "n_activated",
                         "noise_amp", "stagnation_streak", "diversity_n", "burst"],
        "dom_misclass": float(dom_errors / max(n, 1)),
        "overall_mae": float(np.mean(scalar_mae)),
    }
    print(f"[fno] Eval: {n} samples")
    print(f"  Overall MAE: {result['overall_mae']:.4f}")
    print(f"  Dominant misclass: {result['dom_misclass']:.3f}")
    for name, err in zip(result["scalar_names"], result["scalar_mae"]):
        print(f"  {name}: {err:.4f}")
    return result


# ─── CLI ─────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", help="Trajectory JSONL path")
    ap.add_argument("--eval", help="Eval JSONL path")
    ap.add_argument("--model", default="fno_model.pt")
    ap.add_argument("--epochs", type=int, default=300)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--infer", help="Input window .npy for inference")
    ap.add_argument("--split", type=float, default=0.8)
    args = ap.parse_args()

    device = args.device
    if device == "auto":
        device = "cuda" if (TORCH_AVAILABLE and torch.cuda.is_available()) else "cpu"

    print(f"[fno] Device: {device} | Torch: {TORCH_AVAILABLE}", flush=True)

    if args.train:
        entries = load_trajectory(args.train)
        samples = trajectory_to_samples(entries)
        print(f"[fno] {len(samples)} samples from {len(entries)} entries", flush=True)

        if len(samples) < 2:
            print("[fno] Need more data", flush=True)
            return

        split = int(len(samples) * args.split)
        train_s, eval_s = samples[:split], samples[split:]
        model, hist = train_fno(train_s, epochs=args.epochs,
                                batch_size=args.batch_size, device=device)
        save_model(model, args.model, hist, {
            "n_train": len(train_s), "n_eval": len(eval_s),
            "n_entries": len(entries),
        })
        if eval_s:
            evaluate(model, eval_s, device)

    if args.eval and not args.train:
        model, _ = load_model(args.model, device)
        entries = load_trajectory(args.eval)
        samples = trajectory_to_samples(entries)
        evaluate(model, samples, device)

    if args.infer:
        model, _ = load_model(args.model, device)
        X = np.load(args.infer)

        import torch as _t
        X_t = _t.from_numpy(X).unsqueeze(0).to(device)
        scenarios = model.predict_scenarios(X_t)

        print("\n── FNO 4-noise forecast ──")
        for nm in NOISE_MODES:
            pred = scenarios[nm]
            c0 = float(pred[-1, 0])
            c1 = float(pred[-1, 1])
            dom = int(np.argmax(pred[-1, 7:]))
            dn = TYPE_NAMES[dom] if dom < len(TYPE_NAMES) else "?"
            print(f"  [{nm:>6s}] +{HORIZON}: conc={c0:.3f} comp={c1:.3f} dom={dn}")


if __name__ == "__main__":
    main()
