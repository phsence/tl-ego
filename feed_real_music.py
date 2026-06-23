#!/usr/bin/env python
"""feed_real_music.py — F:\音乐 → TL-Ego 感官管道
逐首分析 mp3，提取 7 层感官特征，映射 ego 状态，通过 /contemplate 喂入。
"""

import sys, os, json, subprocess, time, re, glob, argparse, hashlib
from pathlib import Path

WORKSPACE = r"C:\Users\Administrator\.openclaw\workspace"
ANALYZER = os.path.join(WORKSPACE, "song", "audio_sensory_analyzer.py")
PYTHON = r"H:\unsloth_env\Scripts\python.exe"
PROGRESS_FILE = os.path.join(WORKSPACE, "projects", "tl-ego", "feed_progress.json")
MUSIC_EXTS = {".mp3", ".flac", ".wav", ".ogg", ".m4a"}

def scan_music(root):
    """扫描目录下所有音乐文件"""
    files = []
    for ext in MUSIC_EXTS:
        files.extend(glob.glob(os.path.join(root, "**", f"*{ext}"), recursive=True))
        files.extend(glob.glob(os.path.join(root, "**", f"*{ext.upper()}"), recursive=True))
    return sorted(set(files))

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"processed": [], "current": None, "total": 0, "started": None}

def save_progress(p):
    os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2, ensure_ascii=False)

def analyze_file(audio_path, trim=90):
    """运行 analyzer，返回 JSON 结果"""
    cmd = [PYTHON, ANALYZER, audio_path, "--json", "--trim", str(trim)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            print(f"  [ERR] analyzer failed: {result.stderr[:200]}")
            return None
        # output is JSON from --json flag
        data = json.loads(result.stdout.strip())
        return data
    except subprocess.TimeoutExpired:
        print(f"  [ERR] analyzer timeout (>180s)")
        return None
    except json.JSONDecodeError as e:
        print(f"  [ERR] bad JSON: {e}")
        print(f"  stdout[:500]: {result.stdout[:500]}")
        return None

def map_to_ego_sensory(analysis):
    """将 7 层分析结果映射为 TL-Ego 感官输入
    analyzer 输出是 flat keys: waveform, spectrum, harmonic, rhythm, timbre, dynamics, texture
    """
    wf = analysis.get("waveform", {})
    sp = analysis.get("spectrum", {})
    ha = analysis.get("harmonic", {})
    rh = analysis.get("rhythm", {})
    ti = analysis.get("timbre", {})
    dy = analysis.get("dynamics", {})
    tx = analysis.get("texture", {})
    
    bpm = rh.get("bpm", 0) or 0
    rms_energy = wf.get("rms_energy", 0) or 0
    crest_factor = wf.get("crest_factor", 0) or 0
    zcr = wf.get("zero_crossing_rate", {})
    zcr_mean = zcr.get("mean", 0) if isinstance(zcr, dict) else 0
    
    centroid_hz = sp.get("centroid_hz", 0) or 0
    bandwidth_hz = sp.get("bandwidth_hz", 0) or 0
    flatness = sp.get("flatness", 0) or 0
    
    key_name = ha.get("key", "unknown") or "unknown"
    mode = ha.get("mode", "") or ""
    harmonic_ratio = ha.get("harmonic_ratio", 0) or 0
    percussive_ratio = ha.get("percussive_ratio", 0) or 0
    chroma_stability = ha.get("chroma_stability", 0) or 0
    
    beat_regularity = rh.get("beat_regularity", 0) or 0
    onset_density = rh.get("onset_density_per_sec", 0) or 0
    onset_strength = rh.get("onset_strength_mean", 0) or 0
    
    brightness = ti.get("brightness", 0) or 0
    warmth = ti.get("warmth", 0) or 0
    roughness = ti.get("roughness", 0) or 0
    
    dynamic_range_db = dy.get("dynamic_range_db", 0) or 0
    trend = dy.get("trend", "stable") or "stable"
    loudness_events = dy.get("loudness_events_per_sec", 0) or 0
    
    valence = tx.get("emotional_valence", 0.5) or 0.5
    arousal = tx.get("arousal", 0.5) or 0.5
    complexity = tx.get("complexity", 0) or 0
    harmonicity = tx.get("harmonicity", 0) or 0
    density = tx.get("density", "?") or "?"
    brightness_char = tx.get("brightness_character", "?") or "?"
    novelty = tx.get("novelty_rate", 0) or 0
    
    # 映射到 ego 感官通道
    # energy: BPM归一化 + RMS + 动态范围
    energy = min(1.0, max(0.1, (bpm / 200.0) * 0.5 + (rms_energy * 2) * 0.3 + (dynamic_range_db / 80.0) * 0.2))
    
    # mood: 效价 + 调式
    is_major = mode and "major" in str(mode).lower()
    is_minor = mode and "minor" in str(mode).lower()
    mood_base = 0.7 if is_major else 0.4 if is_minor else 0.5
    mood = valence * 0.6 + mood_base * 0.4
    
    # tension: 复杂度 + onset_strength + crest (峰值/均值比)
    tension = min(1.0, max(0.1, complexity * 0.4 + (onset_strength * 2) * 0.3 + (crest_factor / 10.0) * 0.3))
    
    # focus: 频谱带宽紧缩 + 和声比 + 节奏规整
    focus = min(1.0, max(0.1, (1.0 - min(1.0, bandwidth_hz / 5000.0)) * 0.4 + harmonic_ratio * 0.3 + beat_regularity * 0.3))
    
    # harmony: 和声比 + 调性稳定性
    harmony = min(1.0, max(0.1, harmonic_ratio * 0.5 + chroma_stability * 0.5))
    
    # timbre_channel: 亮度 + 温暖度
    timbre_channel = min(1.0, max(0.1, brightness * 0.5 + warmth * 0.5))
    
    sensory = {
        "source": "music",
        "energy": round(energy, 3),
        "mood": round(mood, 3),
        "tension": round(tension, 3),
        "focus": round(focus, 3),
        "harmony": round(harmony, 3),
        "timbre": round(timbre_channel, 3),
        "bpm": round(bpm, 1),
        "key": key_name,
        "valence": round(valence, 3),
        "arousal": round(arousal, 3),
        "dynamic_range_db": round(dynamic_range, 1),
    }
    return sensory

def feed_to_ego(filepath, sensory):
    """通过 /contemplate 喂入 TL-Ego"""
    import urllib.request
    
    filename = os.path.basename(filepath)
    query = (
        f"[音乐感官输入] 刚聆听了 {filename}。"
        f"BPM={sensory['bpm']}, 调式={sensory['key']}, "
        f"效价={sensory['valence']}, 唤醒度={sensory['arousal']}, "
        f"能量={sensory['energy']}, 情绪={sensory['mood']}, "
        f"张力={sensory['tension']}, 专注={sensory['focus']}, "
        f"和声={sensory['harmony']}, 音色={sensory['timbre']}, "
        f"动态范围={sensory['dynamic_range_db']}dB。"
        f"感受这段音乐带来的感官冲击。"
    )
    
    payload = json.dumps({"query": query, "depth": 1}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8766/contemplate",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [ERR] ego API: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="F:\音乐 → TL-Ego 感官管道")
    parser.add_argument("--root", default=r"F:\音乐", help="音乐根目录")
    parser.add_argument("--trim", type=int, default=90, help="分析前 N 秒")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 首 (0=全部)")
    parser.add_argument("--dry-run", action="store_true", help="只扫描不分析")
    parser.add_argument("--reset", action="store_true", help="重置进度")
    parser.add_argument("--list", action="store_true", help="列出已处理文件")
    args = parser.parse_args()
    
    if args.reset:
        save_progress({"processed": [], "current": None, "total": 0, "started": None})
        print("[OK] 进度已重置")
        return
    
    # 扫描
    print(f"Scanning {args.root}...")
    files = scan_music(args.root)
    print(f"Found {len(files)} files")
    
    if len(files) == 0:
        print("[ERR] 没找到音乐文件")
        return
    
    if args.dry_run:
        for f in files[:20]:
            print(f"  {f}")
        if len(files) > 20:
            print(f"  ... +{len(files)-20} more")
        return
    
    # 加载进度
    progress = load_progress()
    if progress["total"] == 0:
        progress["total"] = len(files)
        progress["started"] = time.strftime("%Y-%m-%d %H:%M:%S")
    
    processed_set = set(progress["processed"])
    remaining = [f for f in files if f not in processed_set]
    
    if args.limit > 0:
        remaining = remaining[:args.limit]
    
    print(f"Progress: {len(processed_set)}/{len(files)} done, {len(remaining)} remaining")
    
    for i, fpath in enumerate(remaining):
        fname = os.path.basename(fpath)
        print(f"\n[{i+1}/{len(remaining)}] {fname}")
        
        # 分析
        t0 = time.time()
        analysis = analyze_file(fpath, trim=args.trim)
        if analysis is None:
            continue
        
        dt_analyze = time.time() - t0
        print(f"  Analyze: {dt_analyze:.1f}s")
        
        # 映射
        sensory = map_to_ego_sensory(analysis)
        print(f"  Energy={sensory['energy']} Mood={sensory['mood']} Tension={sensory['tension']} BPM={sensory['bpm']}")
        
        # 喂 ego
        t1 = time.time()
        result = feed_to_ego(fpath, sensory)
        dt_ego = time.time() - t1
        
        if result:
            s2_type = result.get("s2_type", "?")
            s2_sub = result.get("s2_sub_type", "")
            print(f"  Ego: s2={s2_type}/{s2_sub} ({dt_ego:.1f}s)")
        else:
            print(f"  Ego: FAIL ({dt_ego:.1f}s)")
        
        # 记录进度
        progress["processed"].append(fpath)
        progress["current"] = fpath
        save_progress(progress)
        
        # 避免打爆 ego
        time.sleep(1)
    
    print(f"\n[DONE] {len(progress['processed'])}/{progress['total']} songs fed")

if __name__ == "__main__":
    main()