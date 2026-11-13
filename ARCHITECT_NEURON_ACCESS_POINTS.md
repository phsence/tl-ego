# TL-Ego 神经元接入点调研报告

> 只读调研，不做修改。覆盖：神经元类型定义/生成/加载、Neural Grid 数据格式、
> tl_ego_server 工具路由与扩展点、Stage4/Stage8 新模块范例。

---

## 一、神经元类型系统总览

### 1.1 类型枚举与注册

**文件：** `tl_ego_server.py` 第 257–258 行

```python
TYPE_NAMES = ["sensory", "integrator", "decider", "action", "memory",
              "rule_modification", "veto", "doubt", ...]
TYPE_IDS = {t: i for i, t in enumerate(TYPE_NAMES)}
```

这是全局权威枚举。服务器内所有神经元类型过滤、STDP bias 计算、IIT Φ 分区均基于此列表。

**子类型注册表：** `sub_type_registry.json`（project 根目录）

```json
{
  "sensory": ["external_stimulus", "self_state_recollection"],
  "integrator": ["conceptual_synthesis", "self_state_optimization", ...],
  "decider": ["choosing", "planning", "postponing_decision", ...],
  "action": ["execute_task", "executing", ...],
  "memory": ["self_state_recollection"]
}
```

定义了 5 个主类型 × 子行为的映射，供工具路由决策时匹配 domain_hint。

### 1.2 全局神经元数据结构

**文件：** `tl_ego_server.py` 第 261–269 行

```python
NEURONS = []              # list of neuron IDs (from grid)
NEURON_EMBED = {}         # id -> normalized embedding vector (list[float])
NEURON_TYPE = {}          # id -> type string  (e.g. "sensory")
NEURON_IDX = {}           # id -> row index in ng._embeddings_matrix
NEURON_TYPE_CONFIDENCE = {}  # id -> bounded confidence in observed role
NEURONS_MAP = {}          # pre-built full map for AttractorNetwork step
```

所有变量在 `_init_grid()`（第 1045 行）一次性从 Neural Grid 填充。

### 1.3 类型置信度更新 API

**文件：** `tl_ego_server.py` 第 1332–1350 行

```python
def update_neuron_type_confidence(observed_type: str, neuron_ids=None, strength: float = 1.0) -> dict:
```

- 只接受 `TYPE_NAMES` 中的类型，否则映射为 `"unknown"`
- 每个 neuron id 的 confidence 做 bounded update（线性插值）
- 供外部模块（如 Stage4 observe_pattern）调用，用于持续修正类型标签

---

## 二、Neural Grid 数据格式

### 2.1 神经元注册表（neuron_registry.jsonl）

**位置：** `skills/neural_grid/data/neuron_registry.jsonl`  
**大小：** 3.45 GB，170,000 条神经元

每行一个 JSON 对象，格式：

```json
{
  "id": "n-sensory-0000",
  "type": "sensory",
  "embedding": [1.1495, -2.5678, ...],   // 1024-dim float32 向量
  "module_path": "",
  "connections": {},
  "created_at": "2026-04-18T11:03:15.694863"
}
```

**ID 命名规则：**
- `n-sensory-XXXX`（sensory 类型，22000 条）
- `n-integrator-XXXX`（integrator 类型，22000 条）
- `n-decider-XXXX`（decider 类型，22000 条）
- `n-action-XXXX`（action 类型，22000 条）
- `n-memory-XXXX`（memory 类型，22000 条）
- `n-random-XXXXXX`（随机生成，已存在）
- `n-veto-XXXX`、`n-doubt-XXXX`、`n-rule_modification-XXXX`（各 20000 条）

### 2.2 嵌入矩阵缓存（neuron_embeddings.npy + neuron_ids.npy）

**位置：** `skills/neural_grid/data/`  
**大小：** 约 664 MB（npy 缓存）+ 3 MB（ID 列表）

`init_grid()`（`neural_grid.py` 第 467 行）优先从 `.npy` 文件加载；首次从 JSONL 解析后自动缓存。

### 2.3 边数据库（grid_edges.sqlite）

**位置：** `skills/neural_grid/data/grid_edges.sqlite`  
**大小：** 约 20 GB

Schema：
```sql
CREATE TABLE edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    weight REAL DEFAULT 0.0,
    last_update TEXT,
    PRIMARY KEY (src, dst)
);
CREATE INDEX idx_src ON edges(src);
CREATE INDEX idx_dst ON edges(dst);
```

### 2.4 边存储类（EdgeStore）

**文件：** `neural_grid.py` 第 65 行起

核心方法：
| 方法 | 行号 | 说明 |
|------|------|------|
| `EdgeStore.__init__` | ~65 | 初始化 SQLite + WAL 模式 |
| `get_outgoing_edges` | ~220 | 查单个神经元的出边 |
| `batch_all_edges` | ~260 | 批量查一组神经元的入/出边 |
| `ann_search` | ~340 | Faiss ANN 近邻搜索 |
| `build_faiss_index` | ~370 | 构建 Faiss 索引 |
| `preload_partition` | ~270 | 按类型预加载边分区 |

---

## 三、Neural Grid 核心 API

### 3.1 初始化与加载

**`init_grid(preload_partitions=False)`**  
**文件：** `neural_grid.py` 第 467 行  
- 创建数据目录和 SQLite schema
- 从 `.npy` 缓存或 JSONL 加载全部嵌入
- 构建 `_neuron_id_to_index` 映射
- 可选：通过 `EdgeStore.auto_preload_top_types(top_k=3)` 预加载前 3 种类型的分区边

### 3.2 种子神经元查找

**`find_seed_neurons(query_embedding, top_k=3)`**  
**文件：** `neural_grid.py` 第 979 行  
- 全局矩阵点积：`np.dot(_embeddings_matrix, query_vec)` → O(N) 全量扫描
- `np.argpartition(-sims, top_k)` 取 Top-K
- 返回 neuron ID 列表（不含类型信息）

### 3.3 BFS 扩张

| 函数 | 行号 | 说明 |
|------|------|------|
| `bfs_one_hop(seed_ids)` | ~1035 | 1 跳邻居，用 `batch_all_edges` |
| `bfs_two_hop(seed_ids)` | ~1055 | 2 跳扩张，限制 max_candidates |

### 3.4 路由与激活（Phase 7.4）

**`route_and_activate(query_embedding, top_n=100)`**  
**文件：** `neural_grid.py` 第 1252 行

```
seed → 1-hop → 2-hop → score → aggregate → Hebbian update
```

返回：`{"seeds", "activated_count", "aggregated_output", "num_edges_updated"}`

### 3.5 吸引子路由（Phase 8，含 L8）

**`route_with_attractor(query_embedding, ..., use_ann=False, ...)`**  
**文件：** `neural_grid.py` 第 ~1320 行

完整吸引子动力学管线：
1. Seed selection (top-3)
2. BFS 扩张 或 Faiss ANN（`use_ann=True`）
3. 初始评分（相似度 × 入边权重）
4. 多轮传播 + 竞争 + leaky integration → 吸引子
5. 返回最终状态 + 吸引子嵌入 + 指标

### 3.6 神经元注册（Stub）

**`register_neuron(neuron_id, embedding, neuron_type=None)`**  
**文件：** `neural_grid.py` 第 1960 行

当前抛出 `NotImplementedError`。  
**这是新神经元接入的第一个未实现入口。**

---

## 四、tl_ego_server 路由与扩展点

### 4.1 服务器架构

**文件：** `tl_ego_server.py`（13,858 行）  
**端口：** 8766  
**基类：** `http.server.BaseHTTPRequestHandler`（第 12694 行 `class Handler`）

仅实现 `do_GET`（第 12701 行）和 `do_POST`（第 13053 行）。

### 4.2 GET 端点清单

| 端点 | 行号 | 说明 |
|------|------|------|
| `/status` | ~12710 | 全局状态（neurons/edges/STDP/resting/Φ/l0/proprio） |
| `/l8` | ~12748 | L8 Cognitive Lab 状态 |
| `/resting-log` | ~12760 | 静息态轨迹（最近 50 tick） |
| `/debug-stacks` | ~12772 | 线程栈快照 |
| `/scan-workspace` | ~12834 | 工作区扫描 |
| `/action-gateway` | ~12842 | 行动网关状态 |
| `/self-model` | ~12850 | Phase 3.3 自模型（fingerprints/feedback/narrative） |
| `/drives` | ~12908 | Stage 2 内稳态驱动力 |
| `/bdi-snapshot` | ~12918 | Stage 2 BDI 快照 |
| `/autobiography/timeline` | ~12926 | Stage 3 因果自传时间线 |
| `/autobiography/why` | ~12934 | Stage 3 归因解释 |
| `/reasoning_neurons` | ~12948 | Stage 4 推理神经元加载 |
| `/know-yourself` | ~12958 | 自我认知分布 |
| `/long-term-memories` | ~13008 | 长期记忆 |
| `/l7-status` | ~13020 | L7 元进化层状态 |
| `/l7-model` | ~13028 | L7 自模型架构 Phase 2 |
| `/l7-value` | ~13036 | L7 价值对齐门 |
| `/l7-proposals` | ~13044 | L7 自修改提案 |
| `/l7-phase3-status` | ~13052 | L7 进化协调 Phase 3 |

### 4.3 POST 端点清单

| 端点 | 行号 | 说明 |
|------|------|------|
| `/contemplate` | ~13080 | 主推理管线（含 Stage 4 observe_pattern 钩子） |
| `/resting` | ~13128 | 静息态 tick（含视觉/躯体种子注入） |
| `/quantum-experiment` | ~13173 | 量子实验 |
| `/feedback` | ~13240 | 反馈回路（含 Stage 4 apply_feedback 钩子） |
| `/web-search` | ~13353 | Tavily 搜索代理 |
| `/save-checkpoint` | ~13360 | 保存检查点 |
| `/run-many` | ~13366 | 批量查询 |
| `/reset-stdp` | ~13374 | 清空 STDP delta |
| `/find-seeds` | ~13380 | 查找种子神经元 |
| `/inject-sensory` | ~13394 | 外部信号注入感官队列 |
| `/rebuild-faiss` | ~13403 | 重建 Faiss 索引 |
| `/apply-parameter` | ~13413 | Stage 5.5 参数写回（白名单） |
| `/metacontemplate` | ~13453 | 元模板递归 |
| `/noise` | ~13460 | 噪声注入/重置 |
| `/drive` | ~13490 | Stage 2 驱动力调节 |
| `/l7-explore` | ~13520 | L7 即时探索 |

### 4.4 扩展点：新增端点的模式

所有路由在 `do_GET`/`do_POST` 末尾通过 `elif path == "/your-path"` 追加。  
规范模式：

```python
elif path == "/your-path":
    if not INIT_COMPLETE:
        self._send_json({"error": "initializing"}, 503)
        return
    try:
        # ... your logic ...
        self._send_json(result)
    except Exception as e:
        self._send_json({"error": str(e)}, 500)
```

**最小测试入口：** `test_route_fast.py`（project 根目录，见第 4.7 节）

### 4.5 工具路由（tools_registry.py）

**文件：** `tools_registry.py`（1,123 行）  
**导入：** `tl_ego_server.py` 第 171 行

```python
from tools_registry import resolve_tool, resolve_tool_for_comm, TOOLS
```

`TOOLS` 字典（~第 16 行起）定义了 7+ 个工具路由条目：

```python
TOOLS = {
    "deep_probe_unstable": { "goal_type": "deep_probe_unstable", ... },
    "act_on_memory_insight": { "goal_type": "act_on_memory_insight", ... },
    "curiosity_explore":      { "goal_type": "curiosity_explore", ... },
    "integrate_high_comp":    { "goal_type": "integrate_high_comp", ... },
    "causal_lens":            { "goal_type": "causal_lens", ... },
    "neuron_sensory_explore": { "goal_type": "sensory_explore", ... },
    "neuron_action_execute":  { "goal_type": "action_execute", ... },
    "neuron_memory_recall":   { ... },
    ...
}
```

每个条目包含：`goal_type`, `description`, `prompt_template`, `domain_hint`, `trigger_condition`。

**关键函数：** `resolve_tool(state)`（~第 1050 行）根据当前网络状态自动匹配最佳工具。

### 4.6 Stage4 集成点（tl_ego_server.py 内）

| 位置 | 行号 | 说明 |
|------|------|------|
| `/contemplate` 结束时 | ~13135 | `observe_pattern()` 记录推理神经元 |
| `/feedback` 结束时 | ~13270 | `apply_feedback()` 更新推理神经元 |
| `/reasoning_neurons` GET | ~12948 | 加载所有推理神经元 |
| 异步注入 | ~6959 | 每 tick 最多 3 个 creative candidate 异步生成 |

### 4.7 Stage4 测试入口

**文件：** `test_stage4_creative_evoneuron.py`  
覆盖：`load_neurons`, `save_neurons`, `observe_pattern`, `generate_creative_candidates`, `apply_feedback`

```bash
cd projects/tl-ego && python test_stage4_creative_evoneuron.py
```

---

## 五、Stage4 模块（S2 Internalization + Creative EvoNeuron）

### 5.1 文件与结构

**文件：** `stage4_s2_internalization.py`（391 行）  
**持久路径：** `.lae/stage4_reasoning_neurons.json`  
**candidate 日志：** `.lae/stage4_creative_candidates.jsonl`

### 5.2 神经元默认结构

```python
_DEFAULT_NEURON = {
    "state":       { "activation": 0.0, "fatigue": 0.0, "confidence": 0.5, "salience": 0.0 },
    "traits":      { "novelty_sensitivity": 0.4, "risk_sensitivity": 0.3, ... },
    "memory":      { "activation_count": 0, "success_count": 0, ... },
    "prediction":  { "next_type_dist": {}, "error": 0.0, ... },
    "tension":     { "dissonance": 0.0, "uncertainty": 0.0, ... },
    "agency":      { "eligibility": 0.0, "credit": 0.0, "blame": 0.0, ... },
    "creative":    { "association_radius": 0.3, "mutation_rate": 0.05, ... },
    "plasticity":  { "grow_intent": 0.0, "prune_intent": 0.0, ... },
    "development": { "age": 0, "activation_count": 0, "role_entropy": 0.0, ... },
    "meta":        { "confidence": 0.5, "reliability": 0.5, "plasticity": 0.5 },
}
```

### 5.3 关键 API

| 函数 | 行号 | 说明 |
|------|------|------|
| `load_neurons(workspace)` | ~52 | 读取并 _merge_defaults 归一化 |
| `save_neurons(workspace, state)` | ~74 | JSON 持久化 |
| `observe_pattern(workspace, resting_entry, drive_snapshot, outcome_result)` | ~240 | 从 resting tick 记录推理神经元 |
| `generate_creative_candidates(workspace, neuron, ...)` | ~290 | 生成 creative 候选（发散驱动） |
| `apply_feedback(workspace, body)` | ~340 | 从 /feedback 更新神经元 |

### 5.4 observe_pattern 的接入模式

被 `/contemplate`（第 13126 行）和 `/feedback`（第 13298 行）调用。  
输入：`resting_entry`（dominant/concentration/competition/tick）+ `drive_snapshot` + `outcome_result`  
输出： enriched neuron dict 或 `None`

---

## 六、Stage8 模块（Cognitive Lab）

### 6.1 l8_cognitive_lab.py

**文件：** `l8_cognitive_lab.py`（198 行）  
**导入：** `tl_ego_server.py` 第 50 行

```python
from l8_cognitive_lab import (
    rank_mechanism_hypotheses,
    render_contract_test,
    render_diagnostic_mechanism,
    render_integration_guide,
)
```

**核心函数：** `rank_mechanism_hypotheses(problem, state, previously_proposed=None)`（第 31 行）  
- 纯函数，不修改任何状态
- 基于 competition/concentration/stagnation/switches 计算 3 类假说评分：
  - `bounded_observation_window`
  - `transition_diversity_probe`
  - `causal_attribution_probe`
- 返回排序后的假说列表

### 6.2 l8_sandbox_verifier.py

**文件：** `l8_sandbox_verifier.py`（238 行）  
**导入：** `tl_ego_server.py` 第 50 行

```python
from l8_sandbox_verifier import (
    verify_l8_contract_delivery,
    verify_l8_proposal,
    write_l8_promotion_artifact,
)
```

三个阶段：
| 阶段 | 函数 | 说明 |
|------|------|------|
| 证据收集 | `verify_l8_contract_delivery` | 验证候选是否满足合同 |
| 沙盒验证 | `verify_l8_proposal` | 验证提案可行性 |
| 推广 | `write_l8_promotion_artifact` | 写入推广工件 |

### 6.3 L8 状态追踪

**文件：** `tl_ego_server.py` 第 43–54 行

```python
_L8_STATUS = {
    "last_imagination": None,
    "last_curiosity": None,
    "last_synthesis": None,
    "last_imagination_tick": -1,
    "last_curiosity_tick": -1,
    "last_synthesis_tick": -1,
}
```

通过 `/l8` GET 端点暴露；运行时由外部模块写入。

### 6.4 Stage8 测试入口

| 测试文件 | 说明 |
|----------|------|
| `test_l8_cognitive_lab.py` | rank_mechanism_hypotheses 单测 |
| `test_l8_sandbox_verifier.py` | verify_l8_contract_delivery 单测 |

---

## 七、tl_ego_server 导入架构

```
tl_ego_server.py
├── skills/neural_grid  (第 160 行 sys.path 注入, 第 163 行 import)
├── tools_registry      (第 171 行)
├── stage4_s2_internalization (延迟 import, 约 6964 行)
├── l0_meta_observer    (延迟 import)
├── proprioceptive_loop (延迟 import)
├── criticality_regulator   (延迟 import)
├── identity_anchor     (第 27 行)
├── value_check         (第 35 行)
├── l8_cognitive_lab    (第 50 行)
├── l8_sandbox_verifier (第 50 行)
├── l7_init             (第 12 行, L7Orchestrator)
├── memory_bus_tl_ego_bridge (第 62 行)
└── runtime_governor    (延迟 import)
```

**新增模块的接入方式：** 直接 `import your_module` 后，在对应路由处 `from your_module import func` 并调用。

---

## 八、新神经元接入的最小实现建议

### 方案 A：扩展 Neural Grid（推荐用于大规模神经元）

**入口 1：注册新神经元**
```python
# 在 neural_grid.py register_neuron() 中实现（当前抛 NotImplementedError，第 1960 行）
# 或直接写入 neuron_registry.jsonl（JSONL 格式见 §2.1）
# ID 格式：n-{type}-{NNNN} 即可自动被 TYPE_NAMES 识别
```

**入口 2：在 tl_ego_server 中加载**
```python
# _init_grid() 第 1057 行已自动扫描 type 字段
# 只需确保新神经元 ID 含合法 type 字段即可
```

**入口 3：路由时参与竞争**
```python
# route_and_activate / route_with_attractor 自动对所有神经元打分
# 无需额外代码
```

### 方案 B：扩展 Stage4 推理神经元（推荐用于高层认知）

```python
# 1. 在 stage4_s2_internalization.py _DEFAULT_NEURON 添加字段（第 19 行）
# 2. observe_pattern 增强（第 240 行）记录新模式
# 3. generate_creative_candidates 扩展（第 290 行）产生新候选
# 4. 通过 /contemplate → observe_pattern 自动记录
```

### 方案 C：新增 server 端点（推荐用于外部控制）

在 `tl_ego_server.py` 的 `do_GET`/`do_POST` 中追加 `elif path == "/your-new-endpoint"` 分支（见 §4.4）。

### 方案 D：扩展 tools_registry（推荐用于新目标类型）

```python
# 在 tools_registry.py TOOLS 字典中添加条目（第 ~16 行）
# 定义 goal_type / description / prompt_template / trigger_condition
# resolve_tool() 会自动匹配
```

---

## 九、快速参考：文件与行号速查

| 内容 | 文件 | 行号 |
|------|------|------|
| TYPE_NAMES 枚举 | `tl_ego_server.py` | 257 |
| NEURONS/NEURON_EMBED 声明 | `tl_ego_server.py` | 261–269 |
| 全局导入 Neural Grid | `tl_ego_server.py` | 160–163 |
| _init_grid() | `tl_ego_server.py` | ~1045 |
| find_seed_neurons | `neural_grid.py` | 979 |
| route_and_activate | `neural_grid.py` | 1252 |
| route_with_attractor | `neural_grid.py` | ~1320 |
| EdgeStore | `neural_grid.py` | 65 |
| register_neuron (stub) | `neural_grid.py` | 1960 |
| neuron_registry JSONL 格式 | `neural_grid.py` | 557–585 |
| EdgeStore.batch_all_edges | `neural_grid.py` | ~260 |
| do_GET | `tl_ego_server.py` | 12701 |
| do_POST | `tl_ego_server.py` | 13053 |
| /contemplate 路由 | `tl_ego_server.py` | ~13080 |
| /resting 路由 | `tl_ego_server.py` | ~13128 |
| /feedback → Stage4 hook | `tl_ego_server.py` | ~13270 |
| Stage4 observe_pattern call | `tl_ego_server.py` | ~13126 |
| TOOLS 字典 | `tools_registry.py` | ~16 |
| resolve_tool | `tools_registry.py` | ~1050 |
| _DEFAULT_NEURON | `stage4_s2_internalization.py` | 19 |
| load_neurons | `stage4_s2_internalization.py` | ~52 |
| observe_pattern | `stage4_s2_internalization.py` | ~240 |
| generate_creative_candidates | `stage4_s2_internalization.py` | ~290 |
| apply_feedback | `stage4_s2_internalization.py` | ~340 |
| rank_mechanism_hypotheses | `l8_cognitive_lab.py` | 31 |
| verify_l8_contract_delivery | `l8_sandbox_verifier.py` | ~40 |
| L8_STATUS | `tl_ego_server.py` | 43 |
| /l8 endpoint | `tl_ego_server.py` | ~12748 |
| sub_type_registry.json | project root | — |
| neuron_registry.jsonl | skills/neural_grid/data/ | 170K 条 |
| neuron_embeddings.npy | skills/neural_grid/data/ | 664 MB |
| grid_edges.sqlite | skills/neural_grid/data/ | 20 GB |
