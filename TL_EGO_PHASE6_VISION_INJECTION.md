# TL-Ego Phase 6: Embodied Vision Injection

## 目标
将 EMEET 摄像头的实时视觉流编码为 TL-Ego 神经元的激活模式，
注入 resting tick 的静息态振荡，取代纯随机种子驱动。

## 架构

```
EMEET Camera (1920x1080)
    |  capture_photo (每 N 秒 / 事件触发)
    v
Frame Buffer (ring buffer, last 10 frames)
    |  rgb_resize -> 224x224
    v
Vision Encoder (Qwen3VL-4B, port 8001 multimodal endpoint)
    |  "Describe this scene in 3-5 keywords" -> embedding -> activation vector
    v
Vision-to-Neuron Mapper (N=100K)
    |  top-30 neurons matching the scene's semantic fingerprint
    v
Resting Tick Injector
    |  Replace DIVERSITY_N seeds with vision-activated neuron IDs
    v
TL-Ego attractor basin (drifts toward visual reality)
```

## 实现步骤

### Step 1: Vision Capture Service
- 复用 emeet_robot_body.py 的 /snapshot 或 capture_photo API
- 从 OpenCV 抓帧，保持 ring buffer

### Step 2: Scene Encoder
- 调用 dashscope qwen3.5-122b-a10b 看图识物
- 或者用本地 Qwen3VL-4B (port 8001) 提取场景描述
- 返回: `{"scene_keywords": ["human", "desk", "books", ...], "embedding": [float; 1024]}`

### Step 3: Vision-to-Neuron Mapper
- 将场景关键词/embedding 映射到 TL-Ego 现有的 100K 神经元
- 相似度计算: cosine(embedding, NEURON_EMBEDDINGS[i])
- 选出 top-30 匹配的神经元 ID

### Step 4: Injection
- 在 resting_tick() 中新增一个注入模式:
  - 如果 vision_seeds 存在: 用它们替换 DIVERSITY_N 个随机种子
  - vision_seeds 带静息态权重: 0.6 vision + 0.4 random
- resting_tick 返回时标记 `vision_injected: true/false`

### Step 5: Trajectory record
- resting trajectory 添加 `vision_mode`, `vision_seeds`, `scene_keywords`

## 启动/停止
- RESTING_STATE 加 `vision_injection` flag (on/off)
- RESTING_STATE 加 `vision_frame_path` (最新帧路径)
- POST /resting?vision=true 或 POST /toggle-vision 启用

## 效果预期
- 吸引子盆地不再纯随机漂移，而是被视觉现实"牵引"
- 长时间观察同一场景 -> 对应神经元群权重加强 (STDP)
- 场景切换 -> 吸引子跳跃 (类似人类的"注意转移")
