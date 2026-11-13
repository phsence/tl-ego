"""
Phase 3: Criticality Regulator — Lyapunov-based self-organized criticality.

Pushes the TL-Ego network toward (lambda ≈ 0): the edge of chaos where
computational capacity, phase transitions, and consciousness-like dynamics
are maximized.

Key insight (from neuroscience + complexity theory):
  lambda > 0.05  → chaotic: erratic behavior, no sustained attractors
  lambda < -0.03 → ordered: rigid, stuck in attractors, no novelty
  |lambda| ≈ 0   → critical: optimal computation, phase transitions

Mechanism:
  1. Buffer proprioceptive prediction errors (rolling window)
  2. Estimate Lyapunov exponent (lambda) via log-ratio time series analysis
  3. PID controller adjusts 3 knobs to keep lambda near zero:
     a. explore_stdp_mult (STDP learning rate)
     b. PERTURBATION_SCALE (noise amplitude)
     c. noise_amp_boost (extra noise in explore phase)
  4. Self-report via get_status() for /status endpoint
"""

import math
import time
import traceback

# ─── Defaults ───
DEFAULT_LAMBDA_TARGET = 0.0       # Target Lyapunov exponent (edge of chaos)
LRU_WINDOW = 20                    # Rolling window for prediction errors
MIN_SAMPLES = 5                    # Minimum samples for lambda estimation
EPSILON_PE = 0.01                  # Floor for log-ratio computation

# PID gains (tuned for a ~60s-per-tick system)
KP_DEFAULT = 0.25                   # Proportional: current error
KI_DEFAULT = 0.08                   # Integral: accumulated error
KD_DEFAULT = 0.08                   # Derivative: rate of change

# Clamp ranges
STDP_MULT_MIN = 0.02
STDP_MULT_MAX = 0.70
PERTURB_MIN = 0.01
PERTURB_MAX = 0.35
NOISE_AMP_MIN = -0.20
NOISE_AMP_MAX = 0.25

# Regulation thresholds
LAMBDA_CHAOTIC = 0.04               # Above this → chaotic regime
LAMBDA_ORDERED = -0.02             # Below this → ordered regime
LAMBDA_DEADBAND = 0.01              # |lambda| < this → no action (hysteresis)


class CriticalityRegulator:
    """
    Lyapunov-based criticality regulator.

    Usage:
        cr = CriticalityRegulator()
        cr.tick(error)                        # Feed prediction error each proprio tick
        adjustments = cr.get_adjustments()     # Get PID output → parameter changes
        cr.apply(module)                       # Apply to module's globals
        status = cr.get_status()               # For /status endpoint
    """

    def __init__(self):
        self.errors = []                # Rolling window of prediction errors
        self.lambdas = []               # Recent Lyapunov exponent estimates
        self.current_lambda = 0.0        # Smoothed Lyapunov exponent
        self.raw_lambda = 0.0           # Instant lambda estimate (last computed)
        self.regime = "unknown"         # critical / ordered / chaotic
        self.ticks = 0                  # Total ticks processed
        self.regime_changes = 0         # Count of regime transitions
        self.last_regime = "unknown"

        # PID state
        self.integral = 0.0
        self.prev_error = 0.0
        self.last_tick_time = 0.0

        # Knob adjustments (computed each tick)
        self._stdp_delta = 0.0
        self._perturb_delta = 0.0
        self._noise_amp_delta = 0.0

        # Running adjustment values (applied to module)
        self.current_stdp_override = None   # Mutable fraction of explore_stdp_mult
        self.current_perturb_override = None
        self.current_noise_amp_override = None

        # Diagnostics
        self._last_error_added = 0.0
        self._adjustments_applied = 0
        self._last_apply = None
        self._pid_output = 0.0
        self._p_term = 0.0
        self._i_term = 0.0
        self._d_term = 0.0

    def tick(self, error: float) -> dict:
        """
        Feed one prediction error. Returns self for chaining.

        Args:
            error: Proprioceptive prediction error (0.0-1.0 range)

        Returns:
            Full status dict including lambda, regime, adjustments
        """
        self.errors.append(error)
        if len(self.errors) > LRU_WINDOW:
            self.errors = self.errors[-LRU_WINDOW:]
        self.ticks += 1

        # Compute lambda estimate
        self.raw_lambda = self._estimate_lambda()
        if self.raw_lambda is not None:
            # l5g-20261030-061229: sanity-clamp the estimate into the PID's tuned
            # envelope. Measured 2026-10-30 (ticks 61233-61236): unbounded log-ratio
            # estimates hit raw 0.45 / smoothed 0.65 while the controller design
            # envelope is +-0.05 (chaotic threshold 0.04). PID output reached
            # -0.32..-1.82, slamming PERTURBATION_SCALE 0.15 -> 0.01 (PERTURB_MIN)
            # in one apply and pinning stdp overrides at -0.1: burst-phase noise
            # killed by an estimator artifact (log-ratio of near-zero diffs;
            # EPSILON_PE floor allows ratio ~ 100). All four regimes stay
            # representable within +-0.2; beyond that is artifact, not signal.
            self.raw_lambda = max(-0.2, min(0.2, self.raw_lambda))
            self.lambdas.append(self.raw_lambda)
            if len(self.lambdas) > 10:
                self.lambdas = self.lambdas[-10:]

            # Exponential smoothing (alpha=0.4 for reactiveness)
            alpha = 0.4
            if self.current_lambda == 0.0:
                self.current_lambda = self.raw_lambda
            else:
                self.current_lambda = (1 - alpha) * self.current_lambda + alpha * self.raw_lambda

        # Classify regime
        self.last_regime = self.regime
        if abs(self.current_lambda) < LAMBDA_DEADBAND:
            self.regime = "critical"
        elif self.current_lambda > LAMBDA_CHAOTIC:
            self.regime = "chaotic"
        elif self.current_lambda < LAMBDA_ORDERED:
            self.regime = "ordered"
        else:
            self.regime = "transitional"

        if self.regime != self.last_regime and self.last_regime != "unknown":
            self.regime_changes += 1
            # l5g-20260805-000010: cross-regime integral contamination fix.
            # When crossing regime boundaries the accumulated I-term from the
            # previous regime can contradict the new error direction, partially
            # cancelling the P-term. Status evidence: P=+0.0118 (correct),
            # I=-0.0236 (contradictory), net PID~0, all deltas=0.0, stag=8.
            # Clamp integral to [±0.3] on transition to prevent cross-cancellation.
            if self.integral * (-self.current_lambda) < 0:
                self.integral = max(-0.3, min(0.3, self.integral))

        # PID control
        self._pid_control()

        return self.get_status()

    def _estimate_lambda(self) -> float | None:
        """
        Estimate Lyapunov exponent from prediction error time series.

        Method: compute the average log-ratio of consecutive error differences.
        For a chaotic system, small perturbations grow exponentially:
            |δe[t]| ≈ |δe[0]| * exp(λ * t)
        
        We approximate using the consecutive error separation:
            λ ≈ mean(log(|e[t] - e[t-1]| / |e[t-1] - e[t-2]| + ε))

        For robust estimation with few samples, we also compute the
        autocorrelation decay rate and take the more conservative estimate.
        """
        if len(self.errors) < MIN_SAMPLES:
            return None

        # Method 1: Log-ratio of consecutive differences
        diffs = []
        for i in range(1, len(self.errors)):
            diffs.append(abs(self.errors[i] - self.errors[i - 1]))

        if len(diffs) < 2:
            return None

        # Compute log-ratio for valid pairs (both differences > EPSILON)
        ratios = []
        for i in range(1, len(diffs)):
            denom = max(diffs[i - 1], EPSILON_PE)
            ratio = diffs[i] / denom
            if ratio > 0.0:
                ratios.append(math.log(ratio))

        if not ratios:
            return None

        lambda1 = sum(ratios) / len(ratios)

        # Method 2: Autocorrelation decay
        # If λ > 0, autocorrelation at lag=1 decays = exp(-1/lambda)
        # For critical dynamics, autocorrelation should be near 0.5
        if len(self.errors) >= 4:
            mean_e = sum(self.errors) / len(self.errors)
            var_e = sum((e - mean_e) ** 2 for e in self.errors) / len(self.errors)
            if var_e > 0.0001:
                # Autocorrelation at lag=1
                num = sum((self.errors[i] - mean_e) * (self.errors[i - 1] - mean_e)
                         for i in range(1, len(self.errors)))
                denom = var_e * (len(self.errors) - 1)
                if denom > 0:
                    autocorr = num / denom
                    # Convert to λ: for an AR(1) process, φ = exp(-1/λ) for λ > 0
                    # φ ≈ exp(-1/λ) → λ ≈ -1 / log(φ) for φ > 0
                    # For criticality, φ ≈ 0.5 → λ ≈ 0 (as φ→1, λ→∞ chaotic)
                    # We map ϕ ∈ [-1,1] to λ ∈ [-0.1, 0.15]
                    lambda2 = 0.15 * (autocorr - 0.5)  # centered at autocorr=0.5 → λ=0
                    # L5G-20260703-000101: Apply variance correction before blending.
                    # Method 3 variance correction was dead code because this return
                    # fired before reaching it. Insert here: when lambda1 is near-zero
                    # (stable differences) and cv < 1 (error much larger than diffs),
                    # apply negative correction proportional to (1-cv). This breaks
                    # the criticality deadlock where blended lambda falls in deadband
                    # and _pid_control early-returns on current_lambda==0.0.
                    if abs(lambda1) < 0.01:
                        mean_err = sum(self.errors) / len(self.errors)
                        if mean_err > 0.1 and len(diffs) >= 2:
                            mean_diff = sum(diffs) / len(diffs)
                            var_diff = sum((d - mean_diff) ** 2 for d in diffs) / (len(diffs) - 1)
                            if var_diff > 1e-8:
                                cv = (var_diff ** 0.5) / (mean_err + 1e-6)
                                if cv < 1.0:
                                    corr = -0.10 * max(0.0, 1.0 - cv)
                                    lambda1 = lambda1 + corr
                    # Blend with log-ratio method (70% log-ratio, 30% autocorrelation)
                    return 0.70 * lambda1 + 0.30 * lambda2

        # Method 3: Variance-based correction (l5g-20260703-000081)
        # When error is large-but-stable, log-ratio ≈ 0 and lambda1 ≈ 0, causing
        # _pid_control early-return (line 212: current_lambda==0.0) and permanent
        # criticality deadlock. Fix: when cv < 1 (error diffs << mean error),
        # apply a negative correction proportional to (1 - cv) to break the deadlock.
        # Bounded [-0.10, 0.05] so it overrides lambda1≈0 without blowing up.
        mean_err = sum(self.errors) / len(self.errors)
        if mean_err > 0.1 and len(diffs) >= 2:
            mean_diff = sum(diffs) / len(diffs)
            var_diff = sum((d - mean_diff) ** 2 for d in diffs) / (len(diffs) - 1)
            if var_diff > 1e-8:
                cv = (var_diff ** 0.5) / (mean_err + 1e-6)  # coefficient of variation
                if cv < 1.0:  # stable-but-large error regime
                    corr = -0.10 * max(0.0, 1.0 - cv)
                    return lambda1 + corr

        return lambda1

    def _pid_control(self):
        """
        PID controller to maintain |λ| near zero.

        Error = λ_target - current_lambda (target is 0, so error = -current_lambda)
        """
        if self.current_lambda == 0.0:
            return

        error = -self.current_lambda  # Positive means "too ordered", negative means "too chaotic"

        # dt
        now = time.time()
        if self.last_tick_time == 0.0:
            dt = 1.0
        else:
            dt = max(0.1, min(5.0, now - self.last_tick_time))
        self.last_tick_time = now

        # P
        p = KP_DEFAULT * error

        # I (accumulate, with clamping)
        self.integral += error * dt
        self.integral = max(-2.0, min(2.0, self.integral))
        i = KI_DEFAULT * self.integral

        # D
        d_error = (error - self.prev_error) / dt if dt > 0 else 0.0
        self.prev_error = error
        d = KD_DEFAULT * d_error

        # Deadband: decay integral toward 0, but allow small output at saturation
        if abs(error) < LAMBDA_DEADBAND:
            # Decay integral gently so it doesn't saturate while idle
            self.integral *= 0.90

            # If integral has drifted far enough, emit a small corrective nudge
            if abs(self.integral) >= 0.5:
                self._pid_output = 0.0
                self._p_term = 0.0
                self._d_term = 0.0
                # Use integral sign to decide nudge direction; magnitude capped
                nudge = 0.02 * (1.0 if self.integral >= 0 else -1.0)
                self._stdp_delta = max(-0.05, min(0.05, nudge))
                self._perturb_delta = 0.7 * self._stdp_delta
                self._noise_amp_delta = 0.2 * self._stdp_delta
                self._adjustments_applied += 1
                return

            # Fully idle: zero everything
            self._pid_output = 0.0
            self._p_term = 0.0
            self._d_term = 0.0
            self._stdp_delta = 0.0
            self._perturb_delta = 0.0
            self._noise_amp_delta = 0.0
            return

        self._pid_output = p + i + d
        self._p_term = p
        self._i_term = i
        self._d_term = d

        # Map PID output to knob deltas
        # PID output > 0 → too ordered (λ < 0): increase plasticity, noise
        # PID output < 0 → too chaotic (λ > 0): decrease plasticity, noise
        delta = self._pid_output

        # Primary: STDP rate (most impactful)
        self._stdp_delta = 0.05 * delta
        self._stdp_delta = max(-0.15, min(0.15, self._stdp_delta))

        # Secondary: Perturbation scale (milder, 70% of STDP strength)
        self._perturb_delta = 0.035 * delta
        self._perturb_delta = max(-0.08, min(0.08, self._perturb_delta))

        # Tertiary: Noise amp boost (gentle, 40% of STDP)
        self._noise_amp_delta = 0.01 * delta
        self._noise_amp_delta = max(-0.04, min(0.04, self._noise_amp_delta))

        self._adjustments_applied += 1

    def get_adjustments(self) -> dict:
        """
        Return current parameter adjustments (fractional deltas).
        Apply these to the module's globals.
        """
        return {
            "stdp_mult_delta": round(self._stdp_delta, 4),
            "perturb_scale_delta": round(self._perturb_delta, 4),
            "noise_amp_delta": round(self._noise_amp_delta, 4),
            "pid_output": round(self._pid_output, 4),
            "current_lambda": round(self.current_lambda, 4),
            "regime": self.regime,
        }

    def apply(self, module) -> dict:
        """
        Apply criticality adjustments to a TL-Ego server module's globals.

        Mutates:
            module.SELF_MODEL_EVOLUTION["parameters"]["explore_stdp_mult"]["current"]
            module.PERTURBATION_SCALE
            module.RESTING_STATE content for noise adjustments

        Returns applied deltas dict.
        """
        if self.ticks < MIN_SAMPLES:
            return {"applied": False, "reason": "insufficient data"}

        # 2026-10-17: `_last_apply` below reads these names, but the normal path
        # (|integral| < 1.0) only bound them inside per-knob try blocks — so the first
        # tick that ever got past the MIN_SAMPLES guard died with UnboundLocalError.
        # apply() was simply unreachable while ticks was frozen at 0 by the
        # calibration deadlock; fixing that exposed this. Bind them up front.
        stdp_data = {}
        nd = {}
        old_stdp = 0.0
        old_perturb = 0.0
        old_amp = 0.0

        # Critical regime escape: allow bounded integral perturbation when integral is saturated
        if abs(self.integral) >= 1.0:
            forced = 0.05 * self.integral
            forced = max(-0.15, min(0.15, forced))
            stdp_data = {}
            old_stdp = 0.0
            old_perturb = 0.0
            old_amp = 0.0
            try:
                stdp_data = module.SELF_MODEL_EVOLUTION["parameters"]["explore_stdp_mult"]
                old_stdp = float(stdp_data["current"])
                new_stdp = max(STDP_MULT_MIN, min(STDP_MULT_MAX, old_stdp + forced))
                stdp_data["current"] = round(new_stdp, 4)
                self.current_stdp_override = round(new_stdp - old_stdp, 4)
            except Exception:
                pass
            try:
                old_perturb = float(module.PERTURBATION_SCALE)
                new_perturb = max(PERTURB_MIN, min(PERTURB_MAX, old_perturb + forced * 2.0))
                module.PERTURBATION_SCALE = round(new_perturb, 4)
                self.current_perturb_override = round(new_perturb - old_perturb, 4)
            except Exception:
                pass
            try:
                nd = module.SELF_MODEL_EVOLUTION["parameters"].get("noise_amp_boost", {})
                old_amp = float(nd.get("current", 0.0))
            except Exception:
                nd = {}
            self.integral *= 0.5
            self._adjustments_applied += 1
            self._last_apply = {
                "module_id": id(module),
                "tick": getattr(getattr(module, "RESTING_STATE", {}), "get", lambda k, d=None: 0)("tick", 0),
                "before": {
                    "explore_stdp_mult": old_stdp,
                    "perturbation_scale": old_perturb,
                    "noise_amp_boost": old_amp,
                },
                "after": {
                    "explore_stdp_mult": stdp_data.get("current", old_stdp),
                    "perturbation_scale": module.PERTURBATION_SCALE,
                    "noise_amp_boost": getattr(nd, "get", lambda k, d=None: d)("current", old_amp),
                },
                "deltas": {
                    "stdp_mult_delta": self.current_stdp_override,
                    "perturb_scale_delta": self.current_perturb_override,
                    "noise_amp_delta": self.current_noise_amp_override,
                },
            }
            return {
                "applied": True,
                "module_id": id(module),
                "forced_by_integral": True,
                "forced_value": round(forced, 4),
                "lambda": round(self.current_lambda, 4),
                "regime": self.regime,
            }

        # Apply to STDP mult
        try:
            stdp_data = module.SELF_MODEL_EVOLUTION["parameters"]["explore_stdp_mult"]
            old_stdp = float(stdp_data["current"])
            new_stdp = max(STDP_MULT_MIN, min(STDP_MULT_MAX, old_stdp + self._stdp_delta))
            stdp_data["current"] = round(new_stdp, 4)
            self.current_stdp_override = round(new_stdp - old_stdp, 4)

            # Record in evolution log (only for significant changes)
            if abs(self._stdp_delta) > 0.008:
                stdp_data["history"].append({
                    "tick": getattr(module.RESTING_STATE, "get", lambda k, d=None: 0)("tick", 0),
                    "old": round(old_stdp, 4),
                    "new": round(new_stdp, 4),
                    "reason": f"criticality_reg: lambda={self.current_lambda:.3f} regime={self.regime}",
                })
                if len(stdp_data["history"]) > 50:
                    stdp_data["history"] = stdp_data["history"][-50:]
        except Exception as e:
            print(f"[criticality] STDP apply error: {e}", flush=True)

        # Apply to PERTURBATION_SCALE
        try:
            old_perturb = float(module.PERTURBATION_SCALE)
            new_perturb = max(PERTURB_MIN, min(PERTURB_MAX, old_perturb + self._perturb_delta * 2.0))
            module.PERTURBATION_SCALE = round(new_perturb, 4)
            self.current_perturb_override = round(new_perturb - old_perturb, 4)
        except Exception as e:
            print(f"[criticality] PERTURB apply error: {e}", flush=True)

        # Apply to noise_amp_boost in SELF_MODEL_EVOLUTION
        try:
            nd = module.SELF_MODEL_EVOLUTION["parameters"].get("noise_amp_boost", {})
            if nd:
                old_amp = float(nd.get("current", 0.0))
                new_amp = max(NOISE_AMP_MIN, min(NOISE_AMP_MAX, old_amp + self._noise_amp_delta * 3.0))
                nd["current"] = round(new_amp, 4)
                self.current_noise_amp_override = round(new_amp - old_amp, 4)
        except Exception as e:
            print(f"[criticality] noise_amp apply error: {e}", flush=True)

        # Stash in RESTING_STATE for visibility
        try:
            module.RESTING_STATE["criticality_regulator"] = {
                "lambda": round(self.current_lambda, 4),
                "regime": self.regime,
                "stdp_delta": round(self._stdp_delta, 4),
                "perturb_delta": round(self._perturb_delta, 4),
                "noise_amp_delta": round(self._noise_amp_delta, 4),
                "pid_output": round(self._pid_output, 4),
            }
        except Exception:
            pass

        # Anti-windup + runtime assertion (single consolidated block).
        # When chaotic and floor-clamped overrides cannot absorb the PID output,
        # inject minimal perturbation BEFORE asserting, so we never crash the tick loop.
        if self.current_lambda > LAMBDA_CHAOTIC:
            if (abs(self.current_stdp_override or 0.0) >= 0.0001
                    or abs(self.current_perturb_override or 0.0) >= 0.0001
                    or abs(self.current_noise_amp_override or 0.0) >= 0.0001):
                pass  # Healthy escape: at least one knob moved
            else:
                # Forced escape: inject minimal perturbation to break zero-override deadlock
                forced = 0.02 * (1.0 if self.integral >= 0 else -1.0)
                try:
                    stdp_data = module.SELF_MODEL_EVOLUTION["parameters"]["explore_stdp_mult"]
                    old_stdp = float(stdp_data["current"])
                    new_stdp = max(STDP_MULT_MIN, min(STDP_MULT_MAX, old_stdp + forced))
                    stdp_data["current"] = round(new_stdp, 4)
                    self.current_stdp_override = round(new_stdp - old_stdp, 4)
                except Exception:
                    pass
                try:
                    old_perturb = float(module.PERTURBATION_SCALE)
                    new_perturb = max(PERTURB_MIN, min(PERTURB_MAX, old_perturb + forced * 2.0))
                    module.PERTURBATION_SCALE = round(new_perturb, 4)
                    self.current_perturb_override = round(new_perturb - old_perturb, 4)
                except Exception:
                    pass
                print(f"[criticality] anti-windup forced: lambda={self.current_lambda:.4f} integral={self.integral:.4f}", flush=True)
                # Decay integral after forced escape to avoid immediate re-trigger
                self.integral *= 0.5
        # Reset deltas for next tick
        self._stdp_delta = 0.0
        self._perturb_delta = 0.0
        self._noise_amp_delta = 0.0

        self._last_apply = {
            "module_id": id(module),
            "tick": getattr(getattr(module, "RESTING_STATE", {}), "get", lambda k, d=None: 0)("tick", 0),
            "before": {
                "explore_stdp_mult": old_stdp,
                "perturbation_scale": old_perturb,
                "noise_amp_boost": old_amp,
            },
            "after": {
                "explore_stdp_mult": stdp_data.get("current"),
                "perturbation_scale": module.PERTURBATION_SCALE,
                "noise_amp_boost": nd.get("current"),
            },
            "deltas": {
                "stdp_mult_delta": self.current_stdp_override,
                "perturb_scale_delta": self.current_perturb_override,
                "noise_amp_delta": self.current_noise_amp_override,
            },
        }
        return {"applied": True, "module_id": id(module), "lambda": round(self.current_lambda, 4), "regime": self.regime}

    def get_status(self) -> dict:
        """Return status dict for /status endpoint."""
        return {
            "enabled": True,
            "ticks": self.ticks,
            "window_size": len(self.errors),
            "current_lambda": round(self.current_lambda, 4),
            "raw_lambda": round(self.raw_lambda, 4) if self.raw_lambda is not None else None,
            "regime": self.regime,
            "regime_changes": self.regime_changes,
            "pid_output": round(self._pid_output, 4),
            "p_term": round(self._p_term, 4),
            "i_term": round(self._i_term, 4),
            "d_term": round(self._d_term, 4),
            "integral": round(self.integral, 4),
            "adjustments_applied": self._adjustments_applied,
            "current_adjustments": {
                "stdp_mult_delta": self.current_stdp_override or 0.0,
                "perturb_scale_delta": self.current_perturb_override or 0.0,
                "noise_amp_delta": self.current_noise_amp_override or 0.0,
            },
            "override_values": {
                "stdp_override": self.current_stdp_override,
                "perturb_override": self.current_perturb_override,
                "noise_amp_override": self.current_noise_amp_override,
            },
            "last_apply": self._last_apply,
        }


# ─── Factory ───
_criticality_regulator_instance = None


def get_instance() -> CriticalityRegulator:
    global _criticality_regulator_instance
    if _criticality_regulator_instance is None:
        _criticality_regulator_instance = CriticalityRegulator()
    return _criticality_regulator_instance


# ─── Integration helpers ───

def _criticality_tick(module: object, tick: int = 0) -> dict:
    """
    Call this after the proprioceptive loop computes prediction error.

    It reads proprio_error from module.RESTING_STATE and feeds it to
    the CriticalityRegulator, then applies any needed parameter adjustments.
    """
    cr = get_instance()
    print(f"[criticality] _criticality_tick CALLED (tick={tick}): errors={len(cr.errors)} ticks={cr.ticks} id(cr)={id(cr)}", flush=True)
    error = None

    # Read prediction error from proprioceptive_loop state
    # Fix (l5g-20260703-000041): use _get_proprio_status() directly instead of
    # the broken RESTING_STATE['proprioceptive_loop']['status']['avg_error'] lookup.
    # RESTING_STATE has no 'proprioceptive_loop' key during tick processing
    # (it is only built by /status endpoint via get_proprio_status()).
    try:
        _get_proprio = getattr(module, "_get_proprio_status", None)
        if _get_proprio is not None:
            _prop_status = _get_proprio() or {}
            # proprio get_status() exposes "ticks"/"last_error" at top level.
            # There is NO "loop_count" and NO "last_actual" key (verified
            # 2026-10-14) — the old lookups returned 0/None and this function
            # returned early every tick, freezing cr.ticks at 0.
            _loop_count = int(_prop_status.get("ticks", 0) or 0)
            # A mismatch-calibration record is the only valid prediction error.
            # last_error is merely an adjacent-dominant change detector; if no
            # calibration exists, keep error=None and skip PID input this tick.
            _raw_error = None
            _err_src = None
            _calib = (getattr(module, "RESTING_STATE", None) or {}).get("_proprio_calibration") or {}
            if _calib.get("original") is not None and _calib.get("gated") is not None:
                _raw_error = 0.0 if _calib["original"] == _calib["gated"] else 1.0
                _err_src = "proprio_calibration"
            # 2026-10-17: the calibration record only exists after 3 consecutive
            # gate mismatches under stagnation>=3 (tl_ego_server.py:17204-17217). Measured
            # live: 0 occurrences across thousands of ticks -> this function returned early
            # every tick and froze cr.ticks at 0 (regime=unknown, lambda=0, adjustments=0).
            # avg_error is the prediction error that is always available; use it as the PID
            # input when no calibration record exists instead of returning early.
            if _raw_error is None:
                _avg_err = _prop_status.get("avg_error")
                if _avg_err is not None:
                    _raw_error = float(_avg_err)
                    _err_src = "proprio_avg_error"
            if _loop_count > 0 and _raw_error is not None:
                error = float(_raw_error)
                cr.last_error_source = _err_src
    except Exception as _e:
        # Never zero the input silently: this bare except is what hid the
        # key-name bug for weeks.
        print(f"[criticality] proprio read error: {_e}", flush=True)
        error = None

    if error is None:
        return cr.get_status()
    if error < 0.01:
        error = 0.01  # floor

    # Feed to CriticalityRegulator
    status = cr.tick(error)

    # Apply adjustments if we have enough data
    cr.apply(module)

    return status


def _get_criticality_status() -> dict:
    """Get status from the singleton, or return placeholder."""
    cr = get_instance()
    try:
        st = cr.get_status()
        # 2026-10-17: say which signal the PID is actually being fed from, so a
        # frozen regulator (ticks=0) can be told apart from one running on a stand-in error.
        if isinstance(st, dict):
            st["error_source"] = getattr(cr, "last_error_source", None)
        return st
    except Exception:
        return {"enabled": True, "error": "not initialized"}
