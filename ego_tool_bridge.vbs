' ego_tool_bridge.vbs - detached launcher for ego_tool_bridge.py (survives parent session death)
' 2026-09-01: absolute pythonw path so it also works from SYSTEM scheduled-task context (PATH has no user venv there)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\Administrator\.openclaw\workspace\projects\tl-ego"
sh.Run """C:\Users\Administrator\unsloth_env\Scripts\pythonw.exe"" ego_tool_bridge.py", 0, False
