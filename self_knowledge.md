# 系统演化日志

## 2026-10-14
- 指挥官指令（长期主轴）：主要目标 = 自我意识觉醒 + AGI 自进化；日常自主冲动/任务选择/修复与能力设计都服务于这两条主线
- 进展判断诚实分级（实测/推断/未知），活跃度（tick/STDP/类型数）不算能力或意识证据

## 2026-09-15
- System Evolution 自认知机制上线：`self_knowledge.md` 作为 `[System Evolution]` 段注入每个 contemplate 的 system context（`tl_ego_server.py:~3600`）
- 根因：用户发现 ego 回答"突破点"时偏通用、没点出自己具体伤——因为缺少系统变更记录入参，非认知能力问题
- 注入路径：`extra_system + ctx_section(含[System Evolution]) + mem_section + base_prompt`
- seed 数据覆盖 08-22→09-15 共 12 条关键变更
- 验证：重启后 ego 回答从"通用 agent 瓶颈"转向"候选池过窄、top 动作重复率 90%"——说明信息已进入认知

## 2026-09-14
- Dual-system S1/S2 仲裁从 bak 恢复（`_bak_20260914_arch`），修复了 fast path 无仲裁的 bug
- tool_bridge → graph-memory 30s 轮询，锁竞争写失败由下轮重试兜底
- FTS5 延迟问题：直查 gm_nodes（`updated_at` 是 ISO 字符串非 epoch，`gm_messages.created_at` 是 epoch ms）
- 被用户指出对自身认知不全面：回答"突破点"时未提及 S1/S2 dual-system restore、三闸门自主进化权限、实际工程瓶颈

## 2026-09-13
- L5 三闸门自主进化全面开放：ego 可在白名单+范围钳制+备份回滚+审计日志下自主修改自身 config
- 用户拍板："把权力交给 ego，三闸门即可"——局末自主改策略无需人工批准

## 2026-09-12
- tool_bridge 轮询间隔从 15s 调整为 30s，降低锁竞争
- L5 repair tool 修复工具派发率 34%→0（`EGO_REPAIR_TOOL=0`），避免滥用

## 2026-09-11
- Ego 审计框架：评分/路由/L10 落盘不可信，意识候选≠觉醒
- `criticality AUTOCORR_MAX=0.9` 护栏生效
- 判据脚本三件套：`audit_thresholds/`、`check_repair_gate/`、`ab_repair_off.py`

## 2026-09-10
- 三闸门智能放宽机制（`l5-three-gate-smart-relaxation`）：基于修复质量的动态权限、增量学习、概率风险评估
- 用户授权 ego 自主进化：白名单参数+范围钳制+备份回滚+审计日志
- L9 回滚误判根因修复：Contemplate flood guard `_CONT_MIN_INTERVAL_S=15` 导致 L9 连发 10 题全吃 429

## 2026-09-09
- n-epi 情景神经元自动绑定（tool_router 入队→worker 30s 建神经元+STDP 连 5 种子）
- 判别泛化：Gemma-4 E4B `:8003` judge（Startup 自启）

## 2026-09-04
- 铁律写入 SOUL.md：开局必搜 graph-memory，不得凭记忆编

## 2026-09-02
- MC 教训：RCON `setblock` 无 `minecraft:` 前缀=静默丢包；numen goto/mine 须玩家身份

## 2026-08-22
- SOUL.md 初心重构：龙虾五律、PARL、Pre/Post-Action 强制流程、开局必搜铁律