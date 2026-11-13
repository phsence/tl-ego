# grid_edges.sqlite 死重清理设计（2026-08-25，仅设计未执行）

## 现状（已实测）

| 项 | 值 |
|---|---|
| 路径 | `C:\Users\Administrator\.openclaw\skills\neural_grid\data\grid_edges.sqlite` |
| 大小 | 32.87 GB，活跃写入（save-checkpoint 持续更新） |
| 行数 | edges = **150,912,970**；hebbian_active_edges = 0（空表） |
| Schema | `edges(src TEXT, dst TEXT, weight REAL, last_update TIMESTAMP, PK(src,dst))` |
| 索引 | idx_src / idx_dst / idx_edges_src / idx_edges_dst —— **两套重复索引**（白吃 ~30% 空间） |
| ID 家族 | 新 = `n-{type}-{num}`；旧 = 裸数字 `'10680'` 等 |
| 死重 | 随机游走样本 300K：src 旧家族 63.6%；**dst 旧家族 ~100%** |
| 写入方 | tl_ego_server.py:4004 直连 + save-checkpoint；edge_store_v2 只读 fallback |

## 核心判断

1. "死"的定义：边可用 ⇔ 两端都能被当前 runtime 解析。旧数字 ID 是否仍可通过 neuron_ids.npy 序号间接解析 → **Phase 0 必须先验证**，不能想当然。
2. 若 dst 全是旧家族且不可解析，则**全部 1.5 亿边都可能无法命中当前 lookup**——10,088 次 SQLite miss 的根源不是"边不够多"，而是"边的地址空间和当前神经元对不上"。
3. 清理收益：33GB→约 12GB（若只留 both-new），索引减半，冷启动/lookup 提速；风险：写错即毁权威库。

## Phase 0 — 验证（只读，可现在做）

1. **ID 解析路径审计**：读 neural_grid 的 lookup/quarantine 代码，确认旧数字 ID 在什么条件下能命中（有没有 int→neuron 映射残留）。
2. **无偏采样**：LIMIT 前 300K 有 src 排序偏置。改用 `WHERE rowid % 507 == 0`（质数取模）单次全扫采样 ~30 万条，分别统计 src/dst 家族占比 + both-new 占比。
3. **时间分布**：旧家族边的 last_update 是否全部早于某个切换日（预期 2026-08-07 冷层注入修复前后）。若是，佐证死重结论。

## Phase 1 — 清理方案（需停机窗口）

**推荐：重建换名法（B 方案）**
```
0) TL-Ego 停机（restart_tl_ego.ps1 的 kill 段逻辑），确认无进程持有句柄
1) 兜底备份: VACUUM INTO 'grid_edges_backup_YYYYMMDD.sqlite'   ← 或直接文件复制（更快）
2) CREATE TABLE edges_new AS
     SELECT src,dst,weight,last_update FROM edges
     WHERE src LIKE 'n-%' AND dst LIKE 'n-%';        -- 只留 both-new
3) 建 单套 索引 (src) + (dst)，删掉重复的 idx_src/idx_dst 旧命名
4) 事务内 DROP TABLE edges; ALTER TABLE edges_new RENAME TO edges;
5) 启动 TL-Ego → 观察 /status edge_store 数值 + 一次冷层回源验证
6) 观察期 48h 后删除 backup
```
- 预计新表大小：both-new 占比 p × 33GB（Phase 0 定 p；若 dst 全旧则 p 可能 <5%，新库 <2GB）
- 耗时估算：CTAS 单次顺序扫 ≈ 2-4 分钟/百万行量级 → 151M 行预计 **1.5-3 小时**（磁盘 IO 密集）
- 磁盘约束：当前 C 盘剩 **56.2GB** —— 备份(33GB) + 新表(≤12GB) 同盘勉强够但危险；建议备份放其他盘或外置
- 回滚：原文件改名保留，验证不过直接换回

**不推荐**：
- 批量 DELETE + VACUUM：VACUUM 也要整库重写，耗时相同还多一步，锁窗口更长
- 不清理只加过滤：零风险但 21GB+ 死重永续，每次全扫照旧慢

## Phase 2 — 防回归

1. save-checkpoint 写入前过滤非 n-{type}- 格式端点（写入侧守门，杜绝旧家族回流）
2. hebbian_active_edges 空表：确认是否废弃，废弃则 DROP
3. 把"权威库 ID 格式校验"加进 ego L5 world_verify 类任务的检查面

## 待指挥官拍板的决策点

1. Phase 0 现在跑？（只读，~5 分钟）
2. 备份目标盘选哪块？（C 盘 56GB 余量做双份 33GB 太紧）
3. 停机窗口选什么时候？（预计 2-4 小时 TL-Ego 下线，continuity 会兜底但 tick 内状态会丢 checkpoint 后增量）
