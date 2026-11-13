"""fno_train.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: tl_ego_server.py imports FNO / load_model, path = Path(__file__).parent /
"fno_model.pt"; with no file it logs "[fno] fno_train.py not found, using fallback
rotation"; with the file it logs "FNO model loaded from fno_model.pt on cuda" and calls
FNO_MODEL.predict_scenarios(window, horizon). The real checkpoint (06-21, 639 KB,
n_train=776 / n_eval=195 / n_entries=1000 / loss 1.121) pins the architecture exactly:
  config = {input_dim 15, output_dim 15, width 32, window 20, horizon 10,
            n_fourier_layers 4, modes 16}
  keys: pos_enc(1,30,32) | lift(32,15) | fourier_layers.i.weights(16,32,32,2)
        fourier_layers.i.mlp.0(64,32)/mlp.2(32,64)/norm(32) | decoder.0(64,32)/decoder.2(32,64)
        scenario_heads.0..3 (15,32)
so window+horizon=30 explains pos_enc's length, and there are FOUR scenario heads.
This module reproduces those names/shapes so the trained weights load STRICTLY (asserted
in the self-check against the live file).
Judgement (not measured): the original forward order inside a fourier layer and the
scenario naming are unrecoverable from the checkpoint - implemented as the standard
spectral-residual -> MLP -> LayerNorm block with scenario_0..3 keys.
"""

WINDOW, HORIZON, WIDTH, MODES, HIDDEN, N_LAYERS = 20, 10, 32, 16, 64, 4
DEFAULT_CONFIG = {"input_dim": 15, "output_dim": 15, "width": WIDTH, "window": WINDOW,
                  "horizon": HORIZON, "n_fourier_layers": N_LAYERS, "modes": MODES}


def _torch():
    import torch
    return torch


def _build(config=None):
    """Build the parameter-name-compatible module (must match the checkpoint keys)."""
    t = _torch()
    nn = t.nn
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if k in DEFAULT_CONFIG})

    class _FourierLayer(nn.Module):
        def __init__(self, w, m):
            super().__init__()
            self.weights = nn.Parameter(t.randn(m, w, w, 2) * 0.02)
            self.mlp = nn.Sequential(nn.Linear(w, HIDDEN), nn.GELU(), nn.Linear(HIDDEN, w))
            self.norm = nn.LayerNorm(w)

        def _spectral(self, x):
            xf = t.fft.rfft(x, dim=1)                                   # (B,F,C)
            m = min(self.weights.shape[0], xf.shape[1])
            w = t.view_as_complex(self.weights[:m].contiguous())         # (m,C,C)
            out = t.zeros_like(xf)
            out[:, :m, :] = t.einsum("bfc,fco->bfo", xf[:, :m, :], w)
            return t.fft.irfft(out, n=x.shape[1], dim=1)

        def forward(self, x):
            return self.norm(self.mlp(self._spectral(x) + x))

    class _FNO(nn.Module):
        def __init__(self):
            super().__init__()
            self.cfg = cfg
            self.window = cfg["window"]
            self.horizon = cfg["horizon"]
            self.input_dim = cfg["input_dim"]
            self.output_dim = cfg["output_dim"]
            self.pos_enc = nn.Parameter(t.randn(1, cfg["window"] + cfg["horizon"], cfg["width"]) * 0.02)
            self.lift = nn.Linear(cfg["input_dim"], cfg["width"])
            self.fourier_layers = nn.ModuleList(
                [_FourierLayer(cfg["width"], cfg["modes"]) for _ in range(cfg["n_fourier_layers"])])
            self.decoder = nn.Sequential(nn.Linear(cfg["width"], HIDDEN), nn.GELU(),
                                         nn.Linear(HIDDEN, cfg["width"]))
            self.scenario_heads = nn.ModuleList(
                [nn.Linear(cfg["width"], cfg["output_dim"]) for _ in range(4)])

        def _encode(self, x):
            h = self.lift(x)
            T = h.shape[1]
            pe = self.pos_enc
            if pe.shape[1] < T:
                pe = t.cat([pe, pe[:, -1:, :].repeat(1, T - pe.shape[1], 1)], dim=1)
            h = h + pe[:, :T, :]
            for layer in self.fourier_layers:
                h = layer(h)
            return self.decoder(h)

        def forward(self, x):
            return self._encode(x)

        def predict_scenarios(self, window=None, horizon=None):
            """Pad the window to window+horizon, run one FNO pass, and read the horizon tail
            from each of the four heads -> {"scenario_0..3": [[output_dim floats] * horizon]}."""
            t_ = _torch()
            dev = next(self.parameters()).device
            try:
                x = t_.as_tensor(window, dtype=t_.float32)
                if x.dim() == 2:
                    x = x.unsqueeze(0)
                if x.shape[1] < self.window:
                    x = t_.cat([t_.zeros(1, self.window - x.shape[1], x.shape[2], device=x.device), x], dim=1)
                x = x[:, -self.window:, :]
            except Exception:
                x = t_.zeros(1, self.window, self.input_dim)
            x = x.to(dev)
            H = int(horizon or self.horizon)
            inp = t_.cat([x, t_.zeros(1, H, x.shape[2], device=dev)], dim=1)
            with t_.no_grad():
                feats = self._encode(inp)
                out = {}
                for i, head in enumerate(self.scenario_heads):
                    tail = head(feats)[:, -H:, :]
                    out["scenario_%d" % i] = tail[0].detach().cpu().tolist()
            return out

    return _FNO()


class FNO:
    """Factory: `FNO(config=...)` builds the checkpoint-compatible module (torch lazy)."""

    def __new__(cls, config=None, *a, **kw):
        return _build(config)


def load_model(path, device=None):
    """-> (model, meta). Reads the real {"model_state","config",...} layout, plus raw
    state_dict / {"state_dict","meta"} fallbacks. meta["strict"] says whether every
    checkpoint tensor landed in the module."""
    t = _torch()
    dev = device or ("cuda" if t.cuda.is_available() else "cpu")
    try:
        ck = t.load(str(path), map_location=dev, weights_only=False)
    except TypeError:
        ck = t.load(str(path), map_location=dev)
    cfg, sd, extra = {}, None, {}
    if isinstance(ck, dict) and "model_state" in ck:
        sd, cfg = ck["model_state"], dict(ck.get("config") or {})
        extra = {k: ck[k] for k in ("timestamp", "n_train", "n_eval", "n_entries") if k in ck}
    elif isinstance(ck, dict) and "state_dict" in ck:
        sd = ck["state_dict"]
        extra = dict(ck.get("meta") or {})
        cfg = {k: extra[k] for k in DEFAULT_CONFIG if k in extra}
    else:
        sd = ck
    model = _build(cfg)
    missing, unexpected = [], []
    if isinstance(sd, dict):
        res = model.load_state_dict(sd, strict=False)
        missing, unexpected = list(res.missing_keys), list(res.unexpected_keys)
    model.to(dev).eval()
    meta = dict(extra)
    meta.update({"loaded_from": str(path), "device": str(dev), "config": cfg,
                 "strict": (isinstance(sd, dict) and not missing and not unexpected),
                 "missing_keys": missing[:10], "unexpected_keys": unexpected[:10]})
    return model, meta


if __name__ == "__main__":
    import os, tempfile
    try:
        t = _torch()
    except Exception as e:
        print("fno_train selfcheck SKIPPED (no torch: %s)" % e)
        raise SystemExit(0)
    m = FNO()
    x = t.rand(2, WINDOW, 15)
    assert m(x).shape == (2, WINDOW, WIDTH), m(x).shape
    sc = m.predict_scenarios(x[0], horizon=HORIZON)
    assert set(sc) == {"scenario_%d" % i for i in range(4)}, list(sc)
    assert len(sc["scenario_0"]) == HORIZON and len(sc["scenario_0"][0]) == 15
    assert t.isfinite(t.tensor(sc["scenario_0"])).all()
    assert len(m.predict_scenarios(None, 3)["scenario_1"]) == 3
    p = os.path.join(tempfile.mkdtemp(), "rt.pt")
    t.save({"model_state": m.state_dict(), "config": dict(DEFAULT_CONFIG)}, p)
    m2, meta = load_model(p, device="cpu")
    assert meta["strict"] is True, meta
    assert t.allclose(m2(x).cpu(), m(x), atol=1e-6)
    real = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fno_model.pt")
    if os.path.exists(real):
        rm, rmeta = load_model(real, device="cpu")
        assert rmeta["strict"] is True, rmeta
        ck = t.load(real, map_location="cpu", weights_only=False)
        assert t.allclose(rm.lift.weight.detach(), ck["model_state"]["lift.weight"].float()), "lift weights not loaded"
        assert t.allclose(rm.pos_enc.detach(), ck["model_state"]["pos_enc"].float()), "pos_enc not loaded"
        r = rm.predict_scenarios(t.rand(WINDOW, 15), HORIZON)
        assert len(r) == 4 and t.isfinite(t.tensor(r["scenario_0"])).all()
        probe = t.rand(1, WINDOW, 15)
        fresh = FNO()
        d = (rm(probe).detach() - fresh(probe).detach()).abs().mean()
        assert float(d) > 1e-4, "trained weights had no effect"
        print("fno_train selfcheck OK | REAL model strict-load | trained weights active | "
              "n_train=%s | 15ch/%dwin -> 4 scenarios x %d" % (rmeta.get("n_train"), WINDOW, HORIZON))
    else:
        print("fno_train selfcheck OK | roundtrip strict | (no fno_model.pt present)")
