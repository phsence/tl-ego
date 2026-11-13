"""Runnable check for the create_file operation in l5_apply_repair.ps1.

4 cases, each a real pwsh run against a scratch sandbox OUTSIDE ego_sandbox/
(the server's auto-gate globs ego_sandbox/*/repair.json — a test repair.json
there could be applied for real by the live server).

  1. happy path  — stdlib imports + logic + compile/status verification -> auto tier, file created, py_compile OK
  2. refuse overwrite — target already exists -> exit before Gate 1, file untouched
  3. new dependency — `import requests` -> NEW DEPENDENCY warning, never auto-applied
  4. security marker — os.system( -> hard gate, exits, nothing created

Target file lives under projects/tl-ego/ (allowlisted) and is deleted after.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

WS = Path(r"C:\Users\Administrator\.openclaw\workspace")
SCRATCH = Path(r"C:\Users\Administrator\AppData\Local\hermes\cache\scratch\l5_create_test")
PS = r"C:\Program Files\PowerShell\7\pwsh.exe"
SCRIPT = WS / "scripts" / "l5_apply_repair.ps1"
TARGET = WS / "projects" / "tl-ego" / "l5_create_file_probe.py"

MODULE = (
    "import json\n"
    "import re\n"
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def probe(value: str) -> str:\n"
    "    \"\"\"Create-file test probe: normalise and hash-free echo.\"\"\"\n"
    "    if not value:\n"
    "        return ''\n"
    "    return re.sub(r'\\s+', ' ', value).strip()\n"
)


def repair(goal_id, new_text, file_name="projects/tl-ego/l5_create_file_probe.py"):
    return {
        "goal_id": goal_id,
        "tick": 1,
        "diagnosis": "create_file operation test",
        "patches": [
            {
                "file": file_name,
                "description": "test module",
                "operation": "create_file",
                "new_text": new_text,
            }
        ],
        "restart_required": False,
        "verification": {
            "compile_check": True,
            "status_check": True,
            "observe_ticks": 1,
            "tick_interval_sec": 10,
            "success_criteria": [],
        },
    }


def run(case, data):
    sb = SCRATCH / case
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "repair.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Production auto-gate passes {**os.environ, L5_APPROVAL: NO} — a stripped env
    # breaks the python stdlib fetch (and compile check), so inherit here too.
    env = {**os.environ, "L5_APPROVAL": "NO"}
    p = subprocess.run(
        [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         "-SandboxDir", __import__("os").path.relpath(sb, WS).replace("\\", "/"), "-SkipRestart"],
        cwd=str(WS), capture_output=True, timeout=600, env=env,
    )
    out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    return p.returncode, out


def main():
    TARGET.unlink(missing_ok=True)
    fails = []

    # ── case 1: happy path ──
    if TARGET.exists():
        fails.append("case1 precondition: probe file already exists")
    rc, out = run("case1", repair("l5g-TEST-create-001", MODULE))
    ok = (
        "Created: projects/tl-ego/l5_create_file_probe.py" in out
        and "py_compile OK" in out
        and TARGET.exists()
        and "GATE 1 - approved (L" in out
        and "NEW DEPENDENCY" not in out
    )
    print(f"case1 happy-path: {'PASS' if ok else 'FAIL'} (rc={rc})")
    if not ok:
        fails.append("case1")
        print(out[-2500:])
    TARGET.unlink(missing_ok=True)

    # ── case 2: refuse to overwrite ──
    TARGET.write_text("# pre-existing\n", encoding="utf-8")
    rc, out = run("case2", repair("l5g-TEST-create-002", MODULE))
    ok = (
        "refusing to overwrite" in out
        and TARGET.read_text(encoding="utf-8") == "# pre-existing\n"
    )
    print(f"case2 refuse-overwrite: {'PASS' if ok else 'FAIL'} (rc={rc})")
    if not ok:
        fails.append("case2")
        print(out[-1500:])
    TARGET.unlink(missing_ok=True)

    # ── case 3: new dependency ──
    # The warning chain is if/elseif — SECURITY RISK (network read w/o policy)
    # prints instead of NEW DEPENDENCY, and both land in the hard gate. What
    # matters: never auto-applied, never created.
    dep_module = "import requests\n\n\ndef fetch(url):\n    return requests.get(url).text\n"
    rc, out = run("case3", repair("l5g-TEST-create-003", dep_module))
    ok = "New imports: True" in out and "Created:" not in out and "hard gate" in out
    print(f"case3 new-dependency: {'PASS' if ok else 'FAIL'} (rc={rc})")
    if not ok:
        fails.append("case3")
        print(out[-1500:])
    TARGET.unlink(missing_ok=True)

    # ── case 4: security marker ──
    bad_module = "import os\n\n\ndef wipe():\n    os.system('rm -rf /tmp/x')\n    return 1\n"
    rc, out = run("case4", repair("l5g-TEST-create-004", bad_module))
    ok = "Security-sensitive" in out and "Created:" not in out and "hard gate" in out
    print(f"case4 security-marker: {'PASS' if ok else 'FAIL'} (rc={rc})")
    if not ok:
        fails.append("case4")
        print(out[-1500:])
    TARGET.unlink(missing_ok=True)

    # ── case 5: compile fails -> rollback deletes the created file ──
    broken = "import json\n\n\ndef broken(:\n    return 1\n"
    rc, out = run("case5", repair("l5g-TEST-create-005", broken))
    ok = "py_compile FAILED" in out and "Deleted created file" in out and not TARGET.exists()
    print(f"case5 rollback-on-compile-fail: {'PASS' if ok else 'FAIL'} (rc={rc})")
    if not ok:
        fails.append("case5")
        print(out[-1500:])
    TARGET.unlink(missing_ok=True)

    print("RESULT:", "ALL PASS" if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
