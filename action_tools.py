#!/usr/bin/env python3
"""P1: Action tools — the 12 tools ego can ACTUALLY execute.

Distinct from tools_registry.py (which holds 29 cognitive/L5 tools that tell
ego WHAT to think about). These are the body tools — what ego can DO.

Categories:
- ego_internal (5): recall_memory, remember, set_goal, check_status, tts_speak
- minecraft (3): setblock, move, chat
- robot (4): look_at, nod, shake_head, capture_photo

Each tool exposes:
- name: str
- description: str
- parameters: JSON schema (OpenAI-compatible)
- executor: callable (filled in via executor_registry.bind_executors)
"""
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ActionTool:
    name: str
    description: str
    parameters: dict
    category: str
    executor: Optional[Callable] = None

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }


# ── 5 ego internal tools ──────────────────────────────────────────────
ACTION_TOOLS: list[ActionTool] = [
    ActionTool(
        name="recall_memory",
        description="从 ego L4 长期记忆中按查询关键词检索相关记忆节点",
        category="ego_internal",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或自然语言查询"},
                "limit": {"type": "integer", "description": "返回条数上限", "default": 5, "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
    ),
    ActionTool(
        name="remember",
        description="写入一条新记忆到 ego L4 长期记忆层",
        category="ego_internal",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆内容"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "标签列表"},
                "importance": {"type": "number", "description": "重要性 0-1", "minimum": 0, "maximum": 1, "default": 0.5},
            },
            "required": ["content"],
        },
    ),
    ActionTool(
        name="set_goal",
        description="为 ego 设置真实目标（指挥官指令：直接进入 L5 自主执行队列，high 优先级插队，完成后向指挥官汇报）",
        category="ego_internal",
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "目标描述"},
                "priority": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium"},
                "horizon": {"type": "string", "enum": ["short", "medium", "long"], "default": "medium"},
                "kind": {"type": "string", "enum": ["directive", "feedback"],
                         "description": "directive=交代任务；feedback=指挥官指出缺陷/要求改进（进化为改进工单）", "default": "directive"},
            },
            "required": ["goal"],
        },
    ),
    ActionTool(
        name="list_goals",
        description="查询指挥官交代过的任务及其状态（回答'上次交代的事怎么样了'前必须先查此工具，禁止凭记忆编造）",
        category="ego_internal",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回几条", "default": 10},
                "status": {"type": "string", "enum": ["succeeded", "failed", "sandbox_autonomous_running"],
                           "description": "可选状态过滤"},
            },
            "required": [],
        },
    ),
    ActionTool(
        name="check_status",
        description="查询 ego 当前运行状态。除 tick/dominant/neurons/edges/uptime_s 外，live_state 含实时认知状态：stagnation_streak（停滞计数）、competition/concentration（竞争/专注度）、phase、channel_behavioral/channel_arbitration_candidate/channel_observed_raw（三层通道主导）、gate_divergent、comm_motivation。回答自身状态问题必须用这些实时值，禁止凭记忆",
        category="ego_internal",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    ActionTool(
        name="tts_speak",
        description="让 ego 通过 TTS 把文字说出来（XiaoxiaoNeural 语音）",
        category="ego_internal",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要说的文本"},
                "speed": {"type": "number", "description": "语速倍率", "default": 1.0, "minimum": 0.5, "maximum": 2.0},
            },
            "required": ["text"],
        },
    ),
    # ── 3 minecraft tools ──────────────────────────────────────────────
    ActionTool(
        name="setblock",
        description="在 Minecraft 世界中放置一个方块",
        category="minecraft",
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "x 坐标"},
                "y": {"type": "integer", "description": "y 坐标"},
                "z": {"type": "integer", "description": "z 坐标"},
                "block": {"type": "string", "description": "方块 ID（如 stone, oak_planks）"},
            },
            "required": ["x", "y", "z", "block"],
        },
    ),
    ActionTool(
        name="move",
        description="让 ego 移动到 Minecraft 世界指定坐标",
        category="minecraft",
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "目标 x 坐标"},
                "y": {"type": "integer", "description": "目标 y 坐标"},
                "z": {"type": "integer", "description": "目标 z 坐标"},
            },
            "required": ["x", "y", "z"],
        },
    ),
    ActionTool(
        name="chat",
        description="在 Minecraft 世界聊天框发消息",
        category="minecraft",
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "聊天内容"},
            },
            "required": ["message"],
        },
    ),
    # ── 4 robot/camera tools ────────────────────────────────────────────
    ActionTool(
        name="look_at",
        description="让摄像头机器人转头看向指定目标（实体/坐标）",
        category="robot",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "目标实体名 或 'x,y,z' 坐标字符串"},
            },
            "required": ["target"],
        },
    ),
    ActionTool(
        name="nod",
        description="让摄像头机器人点头",
        category="robot",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    ActionTool(
        name="shake_head",
        description="让摄像头机器人摇头",
        category="robot",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    ActionTool(
        name="capture_photo",
        description="摄像头拍一张照片并保存",
        category="robot",
        parameters={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "保存文件名（可选）"},
            },
            "required": [],
        },
    ),
    ActionTool(
        name="make_video",
        description=(
            "用你自己写的旁白，生成一段带配音和字幕的短片（MP4）。"
            "scenes 里每幕给一个画面小标题和若干句旁白，工具会逐句合成语音、按实测时间轴烧字幕、出片并返回文件路径。"
            "想做视频、想表达一段完整叙事时用它。"
        ),
        category="media",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "片头大标题（可选，最多 24 字）"},
                "outro": {"type": "string", "description": "片尾收束句（可选，最多 24 字）"},
                "scenes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "description": "分幕，按顺序播放；每幕 1-6 句旁白",
                    "items": {
                        "type": "object",
                        "properties": {
                            "caption": {"type": "string", "description": "该幕画面上的小标题（可选）"},
                            "photo": {"type": "string", "description": "背景图片路径（可选，绝对路径或 assets/photos/ 下的文件名）"},
                            "lines": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 6,
                                "items": {"type": "string"},
                                "description": "该幕的旁白句子，一句一个元素",
                            },
                        },
                        "required": ["lines"],
                    },
                },
                "voice": {"type": "string", "description": "edge-tts 音色，默认 zh-CN-XiaoxiaoNeural"},
                "size": {"type": "array", "items": {"type": "integer"}, "description": "画面尺寸，如 [1920,1080] 或 [1080,1920]，默认 [1280,720]"},
                "fps": {"type": "integer", "description": "帧率，默认 24", "minimum": 10, "maximum": 60},
            },
            "required": ["scenes"],
        },
    ),
]


def list_action_tools() -> list[dict]:
    """Return all action tools in OpenAI function-calling format."""
    return [t.to_openai_schema() for t in ACTION_TOOLS]


def get_action_tool(name: str) -> Optional[ActionTool]:
    """Get action tool by name."""
    for t in ACTION_TOOLS:
        if t.name == name:
            return t
    return None
