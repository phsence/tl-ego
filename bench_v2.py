"""Run the frozen closed-loop benchmark against a v2 task geometry (2026-10-17).

Why: measured 8/8 -- every v1 start column (y=58..108) is AIR; ego falls to the
world floor at y=37 and the target at y=98..104 sits 21..72 blocks overhead.
Every in-harness failure line carries exactly that dy (`dy_67.00>5.0` for the
spec at y=103 vs floor 37). So v1 measures "can you fly", not "can you walk".

v2 keeps the SAME horizontal legs (same x/z, same threshold_h=3) and only:
  * puts start/target on the actual floor (y=38, one above the floor at 37),
  * widens window_s 40 -> 150 (a brain round is ~30s; measured 78s to reach),
and is explicitly NOT comparable with v1 results.

The frozen v1 module is untouched: specs are patched in memory, then the real
runner .pyc is executed so its arm-load gate / cleanup / ledger logic is reused
verbatim instead of reimplemented.

Usage:
    py -3.12 bench_v2.py --live --dev-limit 4 --holdout-limit 4 \
        --candidate-id reach-v2-001 --candidate-params '{"goal_direct_exec": true}'
"""
import dataclasses
import runpy
import sys
from pathlib import Path

PROJ = Path(r"C:/Users/Administrator/.openclaw/workspace/projects/tl-ego")
sys.path.insert(0, str(PROJ))

FLOOR_Y = 38          # ego lands on the y=37 plane, 8/8 columns measured 2026-10-17
WINDOW_S = 150.0
# The executor reads the window from POLICY params (goal_timeout_s, clamp 10..300),
# not from the spec instance: 12/12 evidence rows still said window_s=40.0 while
# the instance carried window_s=150. Pass it via --candidate-params instead.
# The marker must NOT sit in the walking plane: in the 2026-10-17 run every episode
# that actually arrived (min_d 0.58-0.76) was scored failed with marker_missing --
# ego's body removes the gold block it walks into. v1's markers floated in the air,
# which is why v1 arrivals scored success. Lift the marker 3 above the start plane
# (dy 4 <= dy_max 5) so it is a beacon, not an obstacle.
MARKER_LIFT = 3
N_DEV, N_HOLDOUT = 4, 4


def retarget(spec):
    inst = dict(spec.instance)
    sx, _, sz = inst["start"]
    tx, _, tz = inst["target"]
    inst["start"] = [sx, FLOOR_Y, sz]
    inst["target"] = [tx, FLOOR_Y + MARKER_LIFT, tz]
    inst["goal_timeout_s"] = WINDOW_S
    inst["window_s"] = WINDOW_S
    return dataclasses.replace(spec, instance=inst)


def main():
    import capability_suites.minecraft.tasks as mc

    dev = [retarget(s) for s in mc.dev_specs()[:N_DEV]]
    hold = [retarget(s) for s in mc.holdout_specs()[:N_HOLDOUT]]
    for s in dev + hold:
        print(f"[v2] {s.episode_id} start={s.instance['start']} target={s.instance['target']} "
              f"window={s.instance['window_s']}", flush=True)

    mc.dev_specs = lambda: dev
    mc.holdout_specs = lambda: hold

    sys.argv = [sys.argv[0]] + sys.argv[1:]
    runpy.run_path(str(PROJ / "run_minecraft_benchmark.pyc"), run_name="__main__")


if __name__ == "__main__":
    main()
