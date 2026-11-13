"""2026-11-03: WMI 挂死会杀掉 torch 导入 —— 守卫检查。

本机 WMI provider 会反复挂死 (tasklist / Get-CimInstance / Get-NetTCPConnection 全部数分钟
不返回)。torch 导入路径: platform.machine() -> win32_ver() -> _wmi_query() -> WMI,
所以 WMI 一挂, ego 启动就永久卡在 import torch (py-spy 实证在 platform.py:327)。

tl_ego_server.py 顶部现在把 platform._wmi_query / _win32_ver 钉成常量。本检查验证:
  1. 补丁后 platform.machine() 立即返回, 不碰 WMI
  2. 补丁后 import torch 能完成 (WMI 正在挂的时候也能完成)

跑法: python test_platform_wmi_guard.py
"""
import os
import platform
import time

# 与 tl_ego_server.py 顶部完全相同的守卫。
# 注意 _wmi_query 不能返回 None: platform._get_machine_win32() 会解包它
# ([arch, *_] = _wmi_query('CPU','Architecture')), None 会 TypeError。
platform._wmi_query = lambda *a, **k: "9"
platform._win32_ver = lambda *a, **k: ("10", "10.0.26100", "", "Multiprocessor Free")
platform._get_machine_win32 = lambda: os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64")

t0 = time.time()
machine = platform.machine()
dt_machine = time.time() - t0
assert machine and dt_machine < 2.0, f"platform.machine() 仍然慢/失败: {machine!r} {dt_machine:.2f}s"
print(f"platform.machine()={machine} in {dt_machine:.3f}s (未碰 WMI)")

t0 = time.time()
import torch
dt_torch = time.time() - t0
assert dt_torch < 180, f"import torch 超时 {dt_torch:.0f}s —— WMI 挂死没被绕开"
print(f"import torch ok in {dt_torch:.1f}s (torch {torch.__version__})")

v = platform.win32_ver()
assert isinstance(v, tuple) and len(v) == 4, v
print("OK: WMI 被绕开, torch 导入不再受 WMI 挂死影响")
