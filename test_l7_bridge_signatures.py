"""L7 adapters must call L7's real signatures.

The 2026-11-03 bug: `_adapted_exec_command` passed `readonly=...` to
`l7.exec_command(command, workspace=None, timeout=10)`, so every command-based
verification died with `TypeError: unexpected keyword argument 'readonly'` and
ego answered factual questions from memory instead of checking.

Run: python test_l7_bridge_signatures.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ego_tool_l7_bridge as bridge


def _check(label, result):
    text = str(result)
    assert "unexpected keyword argument" not in text, f"{label} signature drift: {text[:200]}"
    assert result.get("status") != "error" or "L7 unavailable" not in text, f"{label}: {text[:200]}"
    print(f"{label} OK -> {text[:120]}")


def main():
    _check("exec_command echo", bridge._adapted_exec_command("echo hi"))
    _check("exec_command wc", bridge._adapted_exec_command("wc -l < l7/__init__.pyc"))
    _check("file_read", bridge._adapted_file_read("requirements.txt", 3)
           if hasattr(bridge, "_adapted_file_read") else bridge._adapted_file_read("run_tlego_service.bat", 3))
    print("all adapters bind to the real L7 signatures")


if __name__ == "__main__":
    main()
