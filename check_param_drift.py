"""扫探针留下的硬编码旋钮。

事故原型: `COACTIVATION_THRESHOLD = 1  # l5g-20261029-060372: reduced from 2→1 ...`
探针直接改源码常量, 没人回滚, 三周里丢了 68,990 条共激活对。同类还有
`kwta_pool_mult` 10→2。判据: 大写常量 + 数字字面量 + 注释带 l5g-/probe,
且这一行没有 os.environ 出口 (没有出口 = 只能改源码 = 不会自动回滚)。

跑法: python check_param_drift.py   退出码 1 = 有未纳管的探针旋钮
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSIGN = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*=\s*(-?\d+(?:\.\d+)?)\s*(#.*)?$")
PROBE_MARKS = ("l5g-", "probe", "探针")
SKIP_DIRS = {"__pycache__", "node_modules", ".git", "venv", "unsloth_env"}
# 探针/一次性脚本不是运行时配置: APPLY_TICK=62725 这种是"某 tick 执行一次"的固定
# 行为, 不会留在服务里持续生效, 报它等于狼来了。
SKIP_PREFIX = ("l5_", "run_", "probe_", "check_", "test_", "verify_", "_probe", "_watch")

bad = []
for f in sorted(ROOT.rglob("*.py")):
    if any(p in SKIP_DIRS or p.startswith("_bak") for p in f.parts):
        continue
    if f.name == Path(__file__).name or f.name.startswith(SKIP_PREFIX):
        continue
    for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = ASSIGN.match(line.strip())
        if not m:
            continue
        name, val, comment = m.group(1), m.group(2), (m.group(3) or "")
        if not any(t in comment.lower() for t in PROBE_MARKS):
            continue
        if "environ" in line:      # 有环境变量出口 = 可回滚, 不算死旋钮
            continue
        bad.append(f"{f.name}:{i}  {name}={val}  <- {comment.strip()[:70]}")

print(f"命中 {len(bad)} 处『探针改过又没有出口的硬编码旋钮』")
for b in bad:
    print("  " + b)
raise SystemExit(1 if bad else 0)
