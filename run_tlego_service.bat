@echo off
set PYTHONPATH=C:\Users\Administrator\.openclaw\skills\neural_grid;C:\Users\Administrator\.openclaw\workspace
set NO_PROXY=127.0.0.1,localhost
set no_proxy=127.0.0.1,localhost
set /p STEPFUN_API_KEY=<C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\ego_key.txt
set EGO_LLM_API_KEY=%STEPFUN_API_KEY%
rem 2026-09-06: enable GPU attractor (AttractorNetworkGpu, ~7x at 100k scale; auto CPU fallback)
set EGO_GPU_KWTA=0
rem 2026-09-16: GPU SNN hot-pool 侧挂层 (真实 LIF + 时差 STDP, snn_layer.py; 失败自动旁路)
set EGO_GPU_SNN=1
rem 2026-09-16 LTP balance fix: 旧版熟边 LTP:LTD=0.95(结构性偏遗忘, 64%边贴死剪枝线);
rem gain=4 翻转到 1.7~4.2(熟边净增强), 0=恢复旧行为
set EGO_LTP_GAIN=4.0
rem 2026-09-11 A/B: integrator_suppression_repair measured 34.2% of all actions
rem (action_impact.jsonl n=913), fired from the calm stratum (pre_stagnation 0.18),
rem raised stagnation +0.95 there, no concentration gain (-0.019). Its arming
rem detector _is_bistable_integrator_oscillation fires on 73.6% of real windows and
rem on 100% of a shuffled null -> no discriminative power. Set to 1 to restore.
set EGO_REPAIR_TOOL=0
rem 2026-10-30: 大脑模型 step-3.5-flash -> step-5-preview (与 ego.env 保持同步)
set EGO_LLM_MODEL=step-5-preview
rem 2026-09-11: predictive plasticity 转移表落盘 (粗细双键回退见 predictive_plasticity.py;
rem 不设这个变量则纯内存, 重启清零)
set EGO_PRED_STATE=C:\Users\Administrator\.openclaw\workspace\.lae\predictive_plasticity.json
rem 2026-10-08: resting KWTA propagation rounds. Default 3 is too few for a
rem low-edge type's seed to spread before KWTA resolves (measured: 30% seed
rem share plateaued at dead=16). Revert by deleting this line.
# 2026-11-09 tick-cost fix: measured 54-tick median 180s (max 1335s), resting
# tick was paying 12 rounds on stagnation ticks. Keep the knob names in sync with
# ego_keepalive.py (the supervisor that actually spawns the process).
set EGO_STAG_KWTA_ROUNDS=6
set EGO_STDP_MAX_SRCS=400
set EGO_STDP_BUDGET_S=20
set EGO_RESTING_KWTA_ROUNDS=5
rem 2026-11-05: 边镜像总边上限 2M->4M。实测默认 2M 时镜像钉死在帽子上
rem (364 源/199.8w 边), 而单次 KWTA 传播要 450+ 种子源 -> 每次 propagation 必 miss:
rem lazy reload + insert + enforce 驱逐 + 脏边扫描全堵在 _lazy_edge_lock 上,
rem 8 个并发 job 排队把 resting tick 拖到 537-873s (py-spy 双 dump 实锤)。
rem 450 种子 x ~5.5k 边/源 ~= 2.5M, 4M 给 2 个并发 job 的并集留余量 (+~230MB RSS)。
rem 2026-11-05 (第二改): 6M 仍然钉在帽子上 —— total 6.10M 边刚好超过 6M 帽, 每 tick 都驱逐+重载
rem (实测 tick 65717 单轮 8 分钟+, 60% CPU, RSS 4.1GB, 无并发 job 干扰)。12M 让全部源常驻,
rem 消除驱逐抖动; 代价是 RSS 随边集增长, 若 RSS 逼近 6GB 或再出现 MemoryError, 回退到 6000000。
rem 内存紧或想回退: 删掉这行即恢复默认 2000000。
set EGO_EDGE_MIRROR_MAX=12000000
rem 2026-11-03: BGE embeddings (127.0.0.1:8002) is a hard dependency of neural_direct,
rem stage-1 semantic match and episodic recall. When it is down ego does NOT crash --
rem it silently degrades to plain LLM with a single "embedding service unavailable"
rem line (22360 of them in the log), and nothing auto-starts it, so after any reboot
rem the whole semantic path stays dark. Guarantee it here, before ego boots.
netstat -ano | findstr ":8002 " | findstr LISTENING >nul || powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','C:\Users\Administrator\.openclaw\workspace\bge8002.cmd' -WindowStyle Hidden"
rem 2026-11-09 commander: ego 主要做自进化 + 意识觉醒。实测派发 40 个 goal 里
rem create_video 6 / memory_recall 6 / 修复类 0 —— 11-08 directive 只拼进了 goal_text,
rem 没参与调度。这里把被 directive 排到最低的杂活改派到最久没跑的自进化类型。
rem 想改方向: 直接改这两行(goal_type 名, 见 tools_registry.TOOLS)。留空 = 关闭。
rem 不在名单里的类型(ego 自己长出来的 need_*/custom_*)完全不受影响。
set EGO_GOAL_DEMOTE=create_video,memory_recall,stock_investment,workspace_assist,explore_external,web_search,query_memorybus,knowledge_retrieve,sensory_explore,action_execute,ego_arena_select,run_quantum_experiment
set EGO_GOAL_FOCUS=rule_modification_repair,causal_lens,deep_probe_unstable,synthesize_novel_concept,knowledge_upgrade,architect_capability_design,integrator_synthesize,imagine_alternatives,curiosity_explore,consciousness_awakening
rem 自进化类型的冷却窗口(秒)。1200s 实测仍太慢: 三小时里 18 次 type-cooldown hold
rem (knowledge_upgrade 8 / decider_deliberate 5 / integrator 3), 且 decider/curiosity
rem 不在名单里按 7200s 走。300s 让每个类型每 5 分钟可再派一次。
rem 2026-11-10: decider_deliberate 移出 FOCUS — 实测 decision_record.json 全库零消费者,
rem 决策不改变行为(只经 result.json 交接段影响下个 goal), 100% 成功率是 vanity 指标;
rem 移出后回到 7200s 常规冷却, decider 簇 dominant 时仍可自然派发。
set EGO_FOCUS_COOLDOWN_SEC=300
cd /d C:\Users\Administrator\.openclaw\workspace\projects\tl-ego
rem 2026-08-30: L5 integrity guard - refuse to boot on clobbered source (08-29 incident class)
rem 2026-09-30: 源文件从备份还原后 scripts\integrity_guard.py 不在磁盘上 -> 仅在其存在时运行
if exist scripts\integrity_guard.py (
  C:\Users\Administrator\unsloth_env\Scripts\python.exe scripts\integrity_guard.py
  if errorlevel 2 (
    echo [bat] INTEGRITY GUARD FAILED - source unrepairable, operator needed >> logs\integrity_bat.log
    exit /b 2
  )
)
echo REPAIR_TOOL=%EGO_REPAIR_TOOL% >> C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\bat-diag.log 2>&1
echo PRED_STATE=%EGO_PRED_STATE% >> C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\bat-diag.log 2>&1
echo RESTING_ROUNDS=%EGO_RESTING_KWTA_ROUNDS% >> C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\bat-diag.log 2>&1
C:\Users\Administrator\unsloth_env\Scripts\python.exe tl_ego_server.py >> logs\tlego-stdout.log 2>&1
rem 2026-10-13 free-dispatch: world quota off + type throttle only on fixed menu.
rem EGO_WORLD_QUOTA=1 restores the 09-11 forced world share; EGO_TYPE_THROTTLE=all restores the 10-tick cooldown for every type.
