#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TL-Ego Embodied Cognition Daemon — Phase 6

把 EMEET 摄像头的视觉流注入 TL-Ego 的静息态振荡，
让吸引子盆地受到真实世界视觉输入牵引。

管道:
  摄像头拍照 → vision LLM 描述 → BGE-M3 嵌入(1024维)
  → 查 TL-Ego neuron_embed 近邻 → POST /resting { visual_seeds: [...] }

使用方法:
  python embody_daemon.py                     # 默认模式(3s间隔)
  python embody_daemon.py --interval 5        # 5秒间隔
  python embody_daemon.py --once              # 只注入一次
  python embody_daemon.py --dry-run           # 不实际注入，只打印
  python embody_daemon.py --status            # 查看上次注入状态
"""

import sys, os, json, time, base64, random, argparse

# Force UTF-8 for stdout (Windows GBK workaround)
if hasattr(sys, 'stdout') and sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass

import numpy as np
from pathlib import Path
from collections import Counter
from datetime import datetime
import traceback
import urllib.request

# ─── Paths ───
WORKSPACE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKSPACE))
sys.path.insert(0, str(Path.home() / ".openclaw" / "skills" / "neural_grid"))

# ─── Config ───
TL_EGO_URL = "http://127.0.0.1:8766"
BGE_URL = "http://127.0.0.1:8002/v1/embeddings"
VISION_URL = "http://127.0.0.1:8001/v1/chat/completions"
VISION_MODEL = ""  # llama-server ignores model field
VISION_KEY = ""

CAPTURE_INTERVAL = 3       # 默认拍照间隔(秒)
TOP_K_NEURONS = 15         # 注入的神经元数量
NEURON_EMBED_FILE = WORKSPACE / "neural_grid_data_100k_backup" / "neuron_embeddings.npy"
NEURON_IDS_FILE = WORKSPACE / "neural_grid_data_100k_backup" / "neuron_ids.npy"
STATUS_FILE = WORKSPACE / ".lae" / "embody_status.json"

# ─── CameraRobot 集成 ───
# 直接复用 camera_robot_portable 的 capture_photo，避免加载整个框架
import cv2

class MiniCam:
    """轻量摄像头封装，只拍照，不加载 STT/TTS/PTZ"""
    def __init__(self, index=0):
        self.index = index
        self.cap = None

    def capture(self) -> dict:
        """拍一张照片，返回 b64_jpeg 和尺寸"""
        if self.cap is None:
            self.cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
            self.cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
            if not self.cap.isOpened():
                return {"error": f"无法打开摄像头 #{self.index}"}
            time.sleep(0.5)  # 等摄像头就绪
        ret, frame = self.cap.read()
        if not ret:
            return {"error": "无法捕获画面"}
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return {"b64_jpeg": base64.b64encode(buf).decode(),
                "width": frame.shape[1], "height": frame.shape[0]}

    def close(self):
        if self.cap:
            self.cap.release()
            self.cap = None


# ─── API Helpers ───

def _post(url: str, data: dict, timeout=10) -> dict:
    """HTTP POST JSON helper"""
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


def describe_scene(b64_img: str) -> str | None:
    """用 vision LLM 描述画面"""
    payload = {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Describe this scene in detail (2-3 sentences focusing on objects, people, lighting):"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
        ]}],
        "max_tokens": 300,
        "temperature": 0.3,
    }
    resp = _post(VISION_URL, payload, timeout=30)
    if "error" in resp:
        print(f"  [embody] vision LLM error: {resp['error']}", flush=True)
        return None
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return None


def embed_text(text: str) -> list[float] | None:
    """BGE-M3 嵌入文本为 1024 维向量"""
    payload = {
        "input": text,
        "model": "bge-m3-q4_k_m.gguf",
    }
    resp = _post(BGE_URL, payload, timeout=10)
    if "error" in resp:
        print(f"  [embody] BGE-M3 error: {resp['error']}", flush=True)
        return None
    try:
        return resp["data"][0]["embedding"]
    except (KeyError, IndexError):
        return None


def load_neuron_embeds() -> tuple[np.ndarray, list[int]]:
    """加载 TL-Ego 神经元嵌入矩阵和 ID 列表"""
    if not NEURON_EMBED_FILE.exists():
        print(f"[embody] 未找到 {NEURON_EMBED_FILE}，尝试通过 TL-Ego API 获取...", flush=True)
        return None, None

    matrix = np.load(NEURON_EMBED_FILE).astype(np.float32)
    # 归一化
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1
    matrix = matrix / norms

    if NEURON_IDS_FILE.exists():
        ids = np.load(NEURON_IDS_FILE, allow_pickle=True).tolist()
    else:
        ids = list(range(len(matrix)))

    print(f"[embody] 已加载 {len(matrix)} 个神经元嵌入，维度 {matrix.shape[1]}", flush=True)
    return matrix, ids


def find_top_k(embed_matrix: np.ndarray, neuron_ids: list,
               query_vec: list[float], k: int = TOP_K_NEURONS) -> list[int]:
    """余弦相似度搜索 top-K 近邻神经元"""
    qv = np.array(query_vec, dtype=np.float32)
    q_norm = np.linalg.norm(qv)
    if q_norm > 0:
        qv = qv / q_norm
    sims = embed_matrix @ qv  # 矩阵乘法 = 余弦相似度（都已归一化）
    top_k_idx = np.argsort(sims)[-k:][::-1]
    top_ids = [neuron_ids[i] for i in top_k_idx]
    top_scores = [float(sims[i]) for i in top_k_idx]
    print(f"  [embody] top-{k} 相似度: {[round(s, 3) for s in top_scores[:5]]}...", flush=True)
    return top_ids


def inject_seeds(neuron_ids: list[int], dry_run=False) -> dict:
    """POST visual seeds 到 TL-Ego /resting"""
    if dry_run:
        print(f"  [embody] (dry-run) 跳过 POST，种子: {len(neuron_ids)} 个神经元", flush=True)
        return {"status": "dry-run", "seeds": len(neuron_ids)}

    payload = {
        "visual_seeds": neuron_ids,
    }
    result = _post(f"{TL_EGO_URL}/resting", payload, timeout=15)
    return result


def save_status(info: dict):
    """保存注入状态"""
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)


def load_status() -> dict:
    """读取上次注入状态"""
    if STATUS_FILE.exists():
        with open(STATUS_FILE) as f:
            return json.load(f)
    return {}


# ─── Main Pipeline ───

def run_one_pipeline(cam: MiniCam, embed_matrix: np.ndarray,
                     neuron_ids: list, dry_run: bool = False) -> dict:
    """执行一轮：拍照 → 描述 → 嵌入 → 查近邻 → 注入"""
    result = {
        "timestamp": datetime.now().isoformat(),
        "status": "failed",
        "seeds": 0,
    }

    # 1. 拍照
    photo = cam.capture()
    if "error" in photo:
        print(f"  [embody] 拍照失败: {photo['error']}", flush=True)
        result["error"] = photo["error"]
        return result
    print(f"  [embody] 已拍照 ({photo['width']}x{photo['height']})", flush=True)

    # 2. 视觉描述
    desc = describe_scene(photo["b64_jpeg"])
    if not desc:
        print(f"  [embody] 描述失败，跳过", flush=True)
        result["error"] = "description failed"
        return result
    print(f"  [embody] 视觉描述: {desc[:80]}...", flush=True)
    result["description"] = desc[:200]

    # 3. BGE-M3 嵌入
    vec = embed_text(desc)
    if vec is None:
        print(f"  [embody] 嵌入失败，跳过", flush=True)
        result["error"] = "embedding failed"
        return result
    print(f"  [embody] 嵌入向量维度: {len(vec)}", flush=True)

    # 4. 查近邻
    if embed_matrix is not None and neuron_ids is not None:
        top_ids = find_top_k(embed_matrix, neuron_ids, vec)
    else:
        # 无嵌入矩阵时的 fallback：随机选一些神经元
        print(f"  [embody] 无嵌入矩阵，使用随机种子", flush=True)
        return result

    result["seeds"] = len(top_ids)
    result["seed_ids"] = top_ids[:10]  # 只记前10个

    # 5. 注入 TL-Ego
    inject_res = inject_seeds(top_ids, dry_run=dry_run)
    if "error" in inject_res:
        print(f"  [embody] 注入失败: {inject_res['error']}", flush=True)
        result["error"] = str(inject_res.get("error"))
        return result

    result["tl_ego_response"] = inject_res.get("dominant", "ok")
    result["status"] = "ok"
    print(f"  [embody] ✓ 注入完成: {len(top_ids)} 种子 → TL-Ego "
          f"dominant={inject_res.get('dominant', '?')}", flush=True)
    return result


def daemon_loop(interval: int = CAPTURE_INTERVAL, dry_run: bool = False):
    """守护进程主循环"""
    print(f"\n{'='*50}", flush=True)
    print(f"🧠 TL-Ego Embodied Cognition Daemon", flush=True)
    print(f"    拍照间隔: {interval}s | 注入神经元: {TOP_K_NEURONS}", flush=True)
    print(f"    TL-Ego: {TL_EGO_URL} | BGE-M3: {BGE_URL}", flush=True)
    print(f"    Dry-run: {dry_run}", flush=True)
    print(f"{'='*50}\n", flush=True)

    # 初始化摄像头
    cam = MiniCam(index=0)
    embed_matrix, neuron_ids = load_neuron_embeds()
    cycle = 0

    try:
        while True:
            cycle += 1
            print(f"\n[embody] 第 {cycle} 轮 @ {datetime.now().strftime('%H:%M:%S')}", flush=True)
            t0 = time.time()

            info = run_one_pipeline(cam, embed_matrix, neuron_ids, dry_run=dry_run)
            info["cycle"] = cycle
            save_status(info)

            elapsed = time.time() - t0
            wait = max(0, interval - elapsed)
            if wait > 0:
                time.sleep(wait)
    except KeyboardInterrupt:
        print(f"\n[embody] 用户中断", flush=True)
    finally:
        cam.close()
        print(f"[embody] 摄像头已释放", flush=True)


# ─── CLI ───

def main():
    parser = argparse.ArgumentParser(description="TL-Ego 体化认知守护进程")
    parser.add_argument("--interval", "-i", type=int, default=CAPTURE_INTERVAL,
                        help=f"拍照间隔(秒)，默认 {CAPTURE_INTERVAL}")
    parser.add_argument("--once", action="store_true",
                        help="只执行一次，不循环")
    parser.add_argument("--dry-run", action="store_true",
                        help="不实际注入，只打印")
    parser.add_argument("--status", action="store_true",
                        help="查看上次注入状态")
    args = parser.parse_args()

    if args.status:
        status = load_status()
        if not status:
            print("无注入记录")
        else:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        return

    if args.once:
        print(f"\n{'='*50}", flush=True)
        print(f"🧠 TL-Ego 体化认知 — 单次模式", flush=True)
        print(f"{'='*50}\n", flush=True)
        cam = MiniCam(index=0)
        embed_matrix, neuron_ids = load_neuron_embeds()
        info = run_one_pipeline(cam, embed_matrix, neuron_ids, dry_run=args.dry_run)
        save_status(info)
        cam.close()
        print(f"\n结果: {json.dumps(info, ensure_ascii=False, indent=2)}", flush=True)
        return

    daemon_loop(interval=args.interval, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
