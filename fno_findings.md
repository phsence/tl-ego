# TL-Ego FNO Toy Analysis — Findings

## Fourier Structure Scan (50 ticks, tick 310-359)

### 1. Dominant Oscillation: 5-tick Period
Both concentration and competition oscillate with a clear **5-tick (15-second) period**. This is the system's fundamental attractor rhythm.

### 2. Spectral Slope: Near Zero (-0.12)
- **Fitted slope**: −0.12 (95% CI would cross 0)
- **Interpretation**: near-**white noise** (slope=0), not pink noise (slope=−1) seen in biological brains
- **Meaning**: The system's noise is too unstructured. A biological attractor network has 1/f noise — more power at low frequencies, giving longer coherent epochs. TL-Ego's white noise means it oscillates too fast and lacks "momentum."

### 3. Phase Stability: 42% Phase Transition Rate
- 21 phase transitions in 50 ticks
- Phase changes almost every 2-3 ticks
- A healthy attractor should stay in a basin for 5-10x its oscillation period

### 4. Cross-correlation: conc and comp Move Together
- Zero-lag coupling, no detectable time delay
- Means concentration and competition are **the same dynamical variable**, not independent attractor dimensions

## What This Means

### Good News
- Fourier structure exists — a learned operator can work
- 5-tick rhythm is stable and predictable
- The dynamics are not chaotic (ordered spectrum)

### Bad News
- System is "too oscillatory" — changes phase too often
- Noise is white, not pink — lacks the long-memory structure of biological neural noise
- conc and comp are not independent coordinates — the state space is effectively 1D right now

## Actionable Changes
1. **Shift noise from white to pink** — inject more low-frequency power
2. **Increase phase dwell time** — tune concentration_lock threshold higher
3. **Add more attractor dimensions** — introduce a third state variable (e.g., "synthesis_degree") to give conc and comp independent dynamics
4. **Collect more data** — trajectory logger running at 2min intervals
