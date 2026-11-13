# Minecraft capability suite: reach_and_survive (plan Task 2)

任务：把 ego 传送到 `start`，在 `target` 的金块标记附近（水平 3 格内）停下并保持存活。
成功与否**只由 RCON 独立回读判定**（`verifier.py`），绝不采信 bridge/ego 的自述。

## 文件

| 文件 | 作用 |
|---|---|
| `verifier.py` | 独立回读：stdlib RCON 客户端、实体状态读取、标记探测、`verify_reach`。只读，无变更命令。 |
| `tasks.py` | dev/holdout/changed_condition specs、指挥官目标注入、benchmark 执行器工厂、`--probe` 只读校准。 |
| `assets/holdout_instances.json` | hidden 冻结实例（4 holdout + 2 changed_condition），坐标为 2026-09-13 只读探测结果。 |
| `manifest_v1.json` | 冻结清单（asset sha256）。改动 asset 必须同步重算，否则 `validate_manifest` 拒绝。 |

## 运行前提

- 游戏服 25565 + RCON 25575 在线（密码默认 `numen_rcon_2026`，可用 `MC_RCON_PASSWORD` 覆盖）；
- bridge（`tl-ego-bridge.js`）在跑：它每轮读 `C:/tools/mc/bot/ego-goal.txt` 作为指挥官目标，这是任务分发通道；
- **会改动世界**：tp ego + `setblock` 金块标记。标记会留在原地（重复跑同一 episode 覆盖同一点，不累积）。

## 校准

`python tasks.py --probe` — 只读检查 ego 在线状态 + 每条 spec 的 start/target 是否可站立。
地形/建筑改变后重跑，并**一起**重新冻结 `assets/holdout_instances.json` 与 `manifest_v1.json` 的哈希。

## 接入 benchmark

```python
from tasks import make_reach_executor, dev_specs, holdout_specs
from closed_loop_benchmark import run_closed_loop_benchmark
executor = make_reach_executor(client)          # MinecraftRCON(...)
report = run_closed_loop_benchmark(
    executor, candidate_id="reach-001", candidate_params={...},
    dev_episodes=dev_specs(), holdout_episodes=holdout_specs(),
    holdout_manifest_path=MANIFEST_PATH, holdout_asset_root=SUITE_DIR, ...)
```

## 状态

- [x] 代码 + 单测（scripted fake RCON，不触真世界）
- [x] live 只读探测通过：10/10 坐标可站立，ego 实体与标记探针在真实服上可用
- [ ] 首次 live 基准跑：依赖 policy 接线（bridge 读取 policy profile 后，candidate_on/off 才有行为差异）

## 已知校准点（首次 live 跑时留意）

- `tp`：numen 同伴若忽略原生 `/tp`，改用桥同款 `execute as <owner> run numen goto <name> <x> <y> <z>`；
- `start` 与首个决策之间有 1 个 bridge 周期（默认 6s）的延迟，窗口至少 30s；
- `steps` 语义 = RCON 采样次数（决策周期的近似），不是决策条数。
