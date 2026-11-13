#!/usr/bin/env python3
"""ego_video_forge.py — spec-driven narrated video generator (ego's video body).

One file, no project deps: edge-tts (voice) -> exact timeline -> numpy/PIL frames
-> ffmpeg (h264 + aac). Captions are burned into the frames, so there is no
separate subtitle pipeline to keep in sync.

Spec (JSON):
{
  "title":   "大标题",                         # optional, fades in at the start
  "outro":   "收尾的一句",                      # optional, fades in at the end
  "voice":   "zh-CN-XiaoxiaoNeural",           # edge-tts voice
  "rate":    "-5%",                            # edge-tts rate
  "size":    [1280, 720],                      # 1920x1080 for the real thing
  "fps":     24,
  "scenes": [
     {"caption": "画面下方的小标题", "photo": "assets/photos/x.jpg",
      "lines": ["旁白第一句。", "旁白第二句。"]}
  ]
}

Usage:
  python ego_video_forge.py --spec spec.json [--out path.mp4]
  python ego_video_forge.py --self-check          # 3-scene smoke test, asserts output

Subprocess-safe: prints one JSON line on success (status/artifact/duration/size).
"""
import argparse
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SR = 24000
LEAD, TAIL, GAP_IN, GAP_SCENE = 0.55, 0.75, 0.18, 0.55
FONT_DIR = "C:/Windows/Fonts"
WARM, WHITE, DIM = (232, 168, 74), (242, 237, 228), (150, 156, 166)
COOL = (96, 132, 148)


# ── audio ────────────────────────────────────────────────────────────
def _tts_line(text: str, voice: str, rate: str, tmp: str, idx: int) -> np.ndarray:
    import edge_tts

    mp3 = os.path.join(tmp, f"l{idx}.mp3")
    asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(mp3))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", mp3, "-f", "s16le", "-ar", str(SR), "-ac", "1", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def build_audio(spec: dict, tmp: str):
    voice = spec.get("voice") or "zh-CN-XiaoxiaoNeural"
    rate = spec.get("rate") or "+0%"
    gap_in, gap_scene = float(spec.get("gap_in", GAP_IN)), float(spec.get("gap_scene", GAP_SCENE))
    lead, tail = float(spec.get("lead", LEAD)), float(spec.get("tail", TAIL))

    lines, scenes, audio, t = [], [], [np.zeros(int(lead * SR), np.float32)], lead
    i = 0
    for si, sc in enumerate(spec["scenes"]):
        start = t
        for text in sc.get("lines") or []:
            wav = _tts_line(text, voice, rate, tmp, i)
            lines.append({"i": i, "start": round(t, 3), "end": round(t + len(wav) / SR, 3),
                          "text": text, "scene": si})
            audio.append(wav)
            t += len(wav) / SR
            i += 1
            if text is not sc["lines"][-1]:
                audio.append(np.zeros(int(gap_in * SR), np.float32))
                t += gap_in
        scenes.append({"i": si, "start": round(start, 3), "end": round(t, 3)})
        audio.append(np.zeros(int(gap_scene * SR), np.float32))
        t += gap_scene
    audio.append(np.zeros(int(tail * SR), np.float32))
    total = t + tail

    wav = np.concatenate(audio)
    want = int(total * SR)
    wav = np.concatenate([wav, np.zeros(max(0, want - len(wav)), np.float32)])[:want]
    pcm = os.path.join(tmp, "narration.wav")
    with open(pcm, "wb") as f:
        import wave
        with wave.open(f, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes((np.clip(wav, -1, 1) * 32767).astype(np.int16).tobytes())
    return pcm, {"lines": lines, "scenes": scenes, "total": round(total, 3)}


# ── frames ───────────────────────────────────────────────────────────
def _font(size: int, bold: bool = False):
    for name in (("msyhbd.ttc" if bold else "msyh.ttc"), "simhei.ttf"):
        p = os.path.join(FONT_DIR, name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _wrap(draw, text, font, maxw):
    out, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= maxw:
            cur += ch
        else:
            out.append(cur); cur = ch
    if cur:
        out.append(cur)
    return out


def draw_row_kinetic(d, row, x, y, font, fill, t, t0, per=0.022, drift=9,
                     shadow=True, a_mult=1.0):
    """Per-character fade+rise: the line assembles as it is spoken."""
    for i, ch in enumerate(row):
        a = min(1.0, max(0.0, (t - t0 - i * per) / 0.16)) * a_mult
        adv = d.textlength(ch, font=font)
        if a > 0.01:
            dy = int((1 - a) * drift)
            if shadow:
                d.text((x + 2, y + dy + 2), ch, font=font,
                       fill=tuple(int(v * a * 0.5) for v in (10, 10, 12)))
            d.text((x, y + dy), ch, font=font, fill=tuple(int(v * a) for v in fill))
        x += adv


def gradient(w, h, seed=0):
    top = np.array([12 + 3 * (seed % 3), 14 + 2 * (seed % 2), 19 + 6 * (seed % 2)], np.float32)
    bot = np.array([4, 5, 8], np.float32)
    col = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    img = top[None, None, :] * (1 - col) + bot[None, None, :] * col
    img = np.repeat(img, w, axis=1)
    rng = np.random.default_rng(7)          # fixed grain: kills 8-bit banding, doesn't crawl
    img += rng.normal(0, 2.0, img.shape).astype(np.float32)
    return np.clip(img, 0, 255)


def blob_specs(seed: int):
    """Per-scene deterministic blob params for aurora() — baked once, not per frame."""
    rng = np.random.default_rng(31 + seed * 7)
    return [(float(rng.uniform(0.15, 0.85)), float(rng.uniform(0.15, 0.85)),
             float(rng.uniform(0.10, 0.22)), float(rng.uniform(0, 6.283)),
             (WARM, COOL, (108, 84, 60))[i]) for i in range(3)]


def aurora(base, W, H, t, grid, specs):
    """Animated background: the static grainy gradient plus 3 slowly orbiting glow blobs."""
    yy, xx = grid
    F = np.array(base, copy=True)
    for cx0, cy0, sp, ph, col in specs:
        cx = (cx0 + 0.20 * math.sin(t * sp + ph)) * W
        cy = (cy0 + 0.16 * math.cos(t * sp * 1.27 + ph)) * H
        R = (0.42 + 0.10 * math.sin(t * 0.25 + ph)) * max(W, H)
        d = np.hypot(xx - cx, yy - cy) / R
        F += (np.clip(1.0 / (1.0 + 0.55 * d * d), 0, 1) ** 2.4 * 0.15)[..., None] \
            * np.array(col, np.float32)
    return F


def cover_fit(im, w, h, zoom=1.0):
    scale = max(w / im.width, h / im.height) * zoom
    im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def load_photo(path, w, h, zoom):
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return None
    im = cover_fit(im, w, h, zoom)
    a = np.asarray(im).astype(np.float32)
    a = a * 0.72 + 0.06 * a.mean()                      # darken, keep contrast
    a = 255 * (0.55 * a / 255 + 0.45 * a.mean() / 255)  # desaturate toward its mean
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    a = a * np.clip(1.15 - 0.45 * r, 0, 1)[..., None]   # vignette
    a[:, : int(w * 0.34)] *= 0.62                       # left scrim for captions
    a[int(h * 0.78):, :] *= 0.72                        # bottom scrim
    return a.astype(np.uint8)


# ── pictures: procedural web + state cards (the 图 half of 图文并茂) ──
_CND = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = re.compile(r"(百分之[零〇一二三四五六七八九十]{1,3}|零点[零〇一二三四五六七八九]{1,3}"
                  r"|[0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十]{1,3}成多?)")
_TAIL = ("只有", "才", "已经", "大概", "差不多", "大约", "约", "为", "是", "有")
_SPLIT = re.compile(r"[，。、；：！？\s“”（）]")


def _cn_int(s: str) -> int:
    if "十" in s:
        a, _, b = s.partition("十")
        return (_CND[a] if a else 1) * 10 + (_CND[b] if b else 0)
    return int(s) if s.isdigit() else int("".join(str(_CND[c]) for c in s))


def extract_metrics(items):
    """Narration -> [{'label','value','disp','t0'}] one entry per spoken state number.

    Only 0..1 scalars become cards: those are the /status numbers ego is allowed to
    quote, and a stray out-of-range ('33 秒') would draw a bar that means nothing.
    """
    out = []
    for text, t0 in items:
        for m in _NUM.finditer(text):
            tok = m.group(1)
            if tok.startswith("百分之"):
                val = _cn_int(tok[3:]) / 100.0
                disp = f"{_cn_int(tok[3:])}%"
            elif tok.startswith("零点"):
                val = float("0." + "".join(str(_CND[c]) for c in tok[2:]))
                disp = f"{val:g}"
            elif tok.endswith("成") or tok.endswith("成多"):
                cut = 2 if tok.endswith("成多") else 1
                val, disp = _cn_int(tok[:-cut]) / 10.0, tok
            else:
                val, disp = float(tok), tok
            if not 0.0 <= val <= 1.0:
                continue
            label = _SPLIT.split(text[:m.start()])[-1]
            for _ in range(4):
                for s in _TAIL:
                    if label.endswith(s) and len(label) > len(s):
                        label = label[:-len(s)]
            label = label.strip()[-6:]
            if len(label) >= 2:
                out.append({"label": label, "value": val, "disp": disp, "t0": t0})
    return out


def node_field(seed: int, n: int = 24):
    """Deterministic drifting node web — dissolves the flat black background."""
    rng = np.random.default_rng(7919 + seed * 13)
    nodes = [(float(rng.uniform(0.03, 0.97)), float(rng.uniform(0.06, 0.94)),
              float(rng.uniform(0.045, 0.13)), float(rng.uniform(0, 6.283)),
              int(rng.integers(0, 2))) for _ in range(n)]
    edges = []
    for i in range(n):
        near = sorted(((nodes[i][0] - nodes[j][0]) ** 2 + (nodes[i][1] - nodes[j][1]) ** 2, j)
                      for j in range(n) if j != i)[:2]
        edges += [(i, j) for _, j in near]
    return nodes, edges


def _node_pos(nodes, t, W, H):
    return [(int(nx * W + 18 * math.sin(t * 0.33 + ph)),
             int(ny * H + 14 * math.cos(t * 0.27 + ph * 1.7)), tone)
            for nx, ny, r, ph, tone in nodes]


def add_glow(F, nodes, t, amp=0.20):
    H, W = F.shape[:2]
    for (x, y, tone), (nx, ny, r, ph, _t) in zip(_node_pos(nodes, t, W, H), nodes):
        R = max(8, int(r * min(W, H)))
        y0, y1, x0, x1 = max(0, y - R), min(H, y + R), max(0, x - R), min(W, x + R)
        if y1 <= y0 or x1 <= x0:
            continue
        yy, xx = np.ogrid[y0:y1, x0:x1]
        g = np.clip(1 - ((xx - x) ** 2 + (yy - y) ** 2) / (R * R), 0, 1) ** 2
        p = 0.65 + 0.35 * math.sin(t * 1.6 + ph * 3.1)
        F[y0:y1, x0:x1] += (g * p * amp)[..., None] * np.array(COOL if tone else WARM, np.float32)
    return F


def draw_web(d, nodes, edges, t, W, H):
    pos = _node_pos(nodes, t, W, H)
    for i, j in edges:
        d.line((pos[i][0], pos[i][1], pos[j][0], pos[j][1]), fill=(46, 42, 34), width=1)
    for x, y, tone in pos:
        d.ellipse((x - 2, y - 2, x + 2, y + 2),
                  fill=(112, 142, 154) if tone else (150, 128, 88))


# ── illustrated room scene: no character ─────────────────────────────
_ANIME = [    # Character-free room palettes; all mid-dark so text stays legible
    {"wall": (40, 46, 70), "band": (30, 34, 54), "floor": (24, 26, 40),
     "desk": (78, 62, 52), "orb": WARM},      # dusk
     {"wall": (34, 42, 62), "band": (26, 32, 48), "floor": (20, 24, 36),
     "desk": (66, 54, 48), "orb": COOL},       # night
     {"wall": (52, 44, 68), "band": (38, 32, 52), "floor": (28, 24, 40),
     "desk": (84, 66, 60), "orb": (226, 130, 150)},  # dawn
]
INK = (26, 22, 32)


def anime_base(W, H, si, grid):
    """Static illustrated room (wall bands + floor + central glow), cached per scene."""
    pal = _ANIME[si % len(_ANIME)]
    F = np.zeros((H, W, 3), np.float32)
    F[:int(H * 0.60)] = pal["wall"]
    F[int(H * 0.60):int(H * 0.80)] = pal["band"]
    F[int(H * 0.80):] = pal["floor"]
    yy, xx = grid
    d = np.hypot(xx - 0.50 * W, yy - 0.43 * H) / (0.38 * H)
    F += (np.clip(1.0 / (1.0 + 0.9 * d * d), 0, 1) ** 2.2 * 0.34)[..., None] \
        * np.array(pal["orb"], np.float32)
    return F


def draw_anime(img, d, si, t, W, H, web):
    """Character-free scene layer: particles, desk and a large animated monitor."""
    pal = _ANIME[si % len(_ANIME)]
    draw_web(d, web[0], web[1], t, W, H)
    rng = np.random.default_rng(97 + si * 5)
    for i in range(10):
        x0, y0 = float(rng.uniform(0.05, 0.95)), float(rng.uniform(0, 1))
        sp = float(rng.uniform(0.012, 0.03))
        y = (y0 - t * sp) % 1.0
        r = 1 + int(2.2 * (0.4 + 0.6 * math.sin(t * 2 + i)))
        d.ellipse((x0 * W - r, y * H - r, x0 * W + r, y * H + r),
                  fill=tuple(int(c * 0.55) for c in pal["orb"]))
    dy = int(H * 0.66)
    d.rectangle((0, dy, W, dy + int(H * 0.035)), fill=pal["desk"], outline=INK, width=2)
    d.rectangle((0, dy + int(H * 0.035), W, dy + int(H * 0.05)),
                fill=tuple(int(c * 0.6) for c in pal["desk"]))
    mx0, my0, mx1, my1 = int(W * 0.12), int(H * 0.34), int(W * 0.88), int(H * 0.61)
    d.rounded_rectangle((mx0 - 6, my0 - 6, mx1 + 6, my1 + 6), radius=8,
                        fill=(30, 32, 42), outline=INK, width=3)
    mon = img.crop((mx0, my0, mx1, my1))
    draw_web(ImageDraw.Draw(mon), web[0], web[1], t, mx1 - mx0, my1 - my0)
    img.paste(mon, (mx0, my0))
    stand_x = W // 2
    d.rectangle((stand_x - int(W * 0.018), my1 + 6,
                 stand_x + int(W * 0.018), dy), fill=(30, 32, 42))


def anime_still(si, t, W, H):
    """One full character-free scene frame for self-check/probes."""
    grid = np.mgrid[0:H, 0:W].astype(np.float32)
    img = Image.fromarray(np.clip(anime_base(W, H, si, grid), 0, 255).astype(np.uint8))
    draw_anime(img, ImageDraw.Draw(img), si, t, W, H, node_field(si))
    return img


def draw_cards(d, cards, t, W, H, f_lab, f_num):
    """Right-side stat card per number ego just said: slides in, counts up, bar eases."""
    cw, ch, gap = int(W * 0.30), int(H * 0.17), int(H * 0.022)
    x0, y = W - int(W * 0.06) - cw, int(H * 0.26)
    for c in cards:
        k = min(1.0, max(0.0, (t - c["t0"] - 0.15) / 0.7))
        k = 1 - (1 - k) ** 3
        cx = x0 + int((1 - k) * cw * 0.35)               # slide in from the right edge
        d.rounded_rectangle((cx, y, cx + cw, y + ch), radius=int(H * 0.018),
                            fill=(17, 18, 24), outline=(92, 78, 54))
        d.rectangle((cx, y + int(ch * 0.26), cx + 3, y + ch - int(ch * 0.26)), fill=WARM)
        d.text((cx + int(cw * 0.09), y + int(ch * 0.15)), c["label"], font=f_lab, fill=DIM)
        num = c["disp"]
        if k < 1.0 and re.fullmatch(r"[0-9.]+", num):     # count up while easing in
            v = c["value"] * k
            num = f"{v:.2f}" if c["value"] <= 1 else f"{v:.1f}"
        d.text((cx + int(cw * 0.09), y + int(ch * 0.34)), num, font=f_num, fill=WARM)
        bx, bw, by = cx + int(cw * 0.09), cw - int(cw * 0.18), y + int(ch * 0.82)
        d.rectangle((bx, by, bx + bw, by + 4), fill=(44, 42, 40))
        d.rectangle((bx, by, bx + max(2, int(bw * c["value"] * k)), by + 4), fill=WARM)
        y += ch + gap


def render(spec, timeline, out_frames_path):
    W, H = (spec.get("size") or [1280, 720])[:2]
    fps = int(spec.get("fps") or 24)
    total = timeline["total"]
    n_frames = int(round(total * fps))
    anime = bool(spec.get("anime", True))
    bgs = {si: gradient(W, H, si) for si in range(len(spec["scenes"]))}
    grid = np.mgrid[0:H, 0:W].astype(np.float32)
    blobs = {si: blob_specs(si) for si in range(len(spec["scenes"]))}
    abgs = {si: anime_base(W, H, si, grid) for si in range(len(spec["scenes"]))}
    photos = {}
    for si, sc in enumerate(spec["scenes"]):
        if sc.get("photo"):
            p = sc["photo"]
            if not os.path.isabs(p):
                p = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), p)
            photos[si] = p
    webs = {si: node_field(si) for si in range(len(spec["scenes"]))}
    cards = {si: extract_metrics([(l["text"], l["start"]) for l in timeline["lines"] if l["scene"] == si])[:2]
             for si in range(len(spec["scenes"]))}
    f_big, f_cap = _font(int(H * 0.085), True), _font(int(H * 0.045))
    f_sub, f_small = _font(int(H * 0.032)), _font(int(H * 0.026))
    f_lab, f_num = _font(int(H * 0.030)), _font(int(H * 0.072), True)
    line_i = 0

    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
         "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
         out_frames_path],
        stdin=subprocess.PIPE)
    t0 = time.time()
    for fi in range(n_frames):
        t = fi / fps
        si = max(0, next((s["i"] for s in reversed(timeline["scenes"]) if t >= s["start"]), 0))
        sc = spec["scenes"][si]
        zoom = 1.0 + 0.04 * ((t - timeline["scenes"][si]["start"]) / max(1e-3, timeline["scenes"][si]["end"] - timeline["scenes"][si]["start"]))
        ph = load_photo(photos[si], W, H, zoom) if si in photos else None
        if ph is not None:
            F = ph.astype(np.float32).copy()
        elif anime:
            F = abgs[si].copy()
        else:
            F = aurora(bgs[si], W, H, t, grid, blobs[si])
        nodes, edges = webs[si]
        if ph is None and not anime:
            F = add_glow(F, nodes, t)
        for s in timeline["scenes"][1:]:                 # dip + light sweep between scenes
            dt = t - s["start"]
            if abs(dt) < 0.36:
                F *= 0.62 + 0.38 * (abs(dt) / 0.36)
                p = (dt + 0.36) / 0.72                   # band crosses the frame over the dip
                bx, bw = int(p * W), int(W * 0.10)
                x0, x1 = max(0, bx - bw), min(W, bx + bw)
                if x1 > x0:
                    band = (1 - np.abs(np.linspace(-1, 1, x1 - x0, dtype=np.float32)) ** 2) * 46
                    F[:, x0:x1] += band[None, :, None]
        img = Image.fromarray(np.clip(F, 0, 255).astype(np.uint8))
        d = ImageDraw.Draw(img)
        if ph is None:
            if anime:
                draw_anime(img, d, si, t, W, H, webs[si])
            else:
                draw_web(d, nodes, edges, t, W, H)

        outro_visible = bool((spec.get("outro") or "").strip()) and t > total - 4.2

        # scene caption (upper-left), fades in over 0.6s
        cap = (sc.get("caption") or "").strip()
        if cap and not outro_visible:
            a = min(1.0, max(0.0, (t - timeline["scenes"][si]["start"]) / 0.6))
            d.text((int(W * 0.07), int(H * 0.14)), cap, font=f_sub,
                   fill=tuple(int(c * a) for c in DIM))

        # state cards: one per number ego actually said in this scene
        live = [c for c in cards[si] if t >= c["t0"] and not (spec.get("title") and t < 3.4)]
        if live:
            draw_cards(d, live, t, W, H, f_lab, f_num)

        # per-line burnt subtitle (bottom centre) — kinetic: assembles per character
        cur = [l for l in timeline["lines"] if l["start"] - 0.10 <= t <= l["end"] + 0.30]
        if cur:
            txt = cur[0]["text"]
            rows = _wrap(d, txt, f_cap, int(W * 0.72))
            fade = min(1.0, max(0.0, (cur[0]["end"] + 0.30 - t) / 0.25))
            y = int(H * 0.84) - (len(rows) - 1) * int(H * 0.055)
            for r in rows:
                wpx = d.textlength(r, font=f_cap)
                draw_row_kinetic(d, r, (W - wpx) / 2, y, f_cap, WHITE, t, cur[0]["start"],
                                 a_mult=fade)
                y += int(H * 0.058)

        # start title card — kinetic per-character assembly
        title, outro = (spec.get("title") or "").strip(), (spec.get("outro") or "").strip()
        if title and t < 3.4:
            a = min(1.0, t / 0.5) * min(1.0, max(0.0, (3.4 - t) / 0.6))
            rows = _wrap(d, title, f_big, int(W * 0.8))
            y = int(H * (0.17 if anime else 0.44)) - (len(rows) - 1) * int(H * 0.05)
            for r in rows:
                wpx = d.textlength(r, font=f_big)
                draw_row_kinetic(d, r, (W - wpx) / 2, y, f_big, WARM, t, 0.05,
                                 per=0.035, drift=16, shadow=False, a_mult=a)
                y += int(H * 0.1)

        # outro card — kinetic per-character assembly
        if outro and t > total - 4.2:
            a = min(1.0, (t - (total - 4.2)) / 0.8)
            rows = _wrap(d, outro, f_big, int(W * 0.8))
            y = int(H * (0.17 if anime else 0.42)) - (len(rows) - 1) * int(H * 0.05)
            for r in rows:
                wpx = d.textlength(r, font=f_big)
                draw_row_kinetic(d, r, (W - wpx) / 2, y, f_big, WHITE, t, total - 4.2,
                                 per=0.035, drift=16, shadow=False, a_mult=a)
                y += int(H * 0.1)

        if fi % 120 == 0:
            print(f"[forge] frame {fi}/{n_frames} {time.time()-t0:.0f}s", flush=True)
        proc.stdin.write(np.asarray(img, dtype=np.uint8).tobytes())
        line_i += 1
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg video encode failed")
    return n_frames


# ── top level ────────────────────────────────────────────────────────
def forge(spec: dict, out_path: str) -> dict:
    if not spec.get("scenes"):
        raise ValueError("spec.scenes is empty")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="forge_")
    try:
        wav, timeline = build_audio(spec, tmp)
        silent = os.path.join(tmp, "video.mp4")
        n_frames = render(spec, timeline, silent)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", wav,
             "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000",
             "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", "-shortest", out_path],
            check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,size", "-of", "json", out_path],
        capture_output=True, text=True, check=True)
    fmt = json.loads(probe.stdout)["format"]
    return {"status": "ok", "artifact": out_path, "frames": n_frames,
            "duration": round(float(fmt["duration"]), 2), "bytes": int(fmt["size"]),
            "lines": len(timeline["lines"]), "scenes": len(spec["scenes"])}


SELF_CHECK_SPEC = {
    "title": "自检", "outro": "能出声，能出画。", "size": [640, 360], "fps": 20,
    "scenes": [
        {"caption": "第一幕", "lines": ["这是自检的第一句。"]},
        {"caption": "第二幕", "lines": ["第二句，验证时间轴。", "竞争强度零点八一。"]},
    ],
}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec")
    ap.add_argument("--out")
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        m = extract_metrics([("竞争强度零点九一", 0.0), ("集中度只有一成多", 1.0),
                             ("预测误差零点四七", 2.0), ("竞争强度 3.2", 3.0)])
        assert [c["label"] for c in m] == ["竞争强度", "集中度", "预测误差"], m
        assert abs(m[0]["value"] - 0.91) < 1e-6 and abs(m[2]["value"] - 0.47) < 1e-6, m
        assert m[1]["disp"] == "一成多" and abs(m[1]["value"] - 0.1) < 1e-6, m
        # the animation layers must actually move — a frozen effect passes visual review
        W2, H2 = 320, 180
        g2 = np.mgrid[0:H2, 0:W2].astype(np.float32)
        sp2 = blob_specs(0)
        b2 = gradient(W2, H2, 0)
        assert np.abs(aurora(b2, W2, H2, 6.0, g2, sp2) - aurora(b2, W2, H2, 0.0, g2, sp2)).mean() > 0.5, \
            "aurora background frozen"
        fnt = _font(20)
        i1, i2 = Image.new("RGB", (W2, H2)), Image.new("RGB", (W2, H2))
        draw_row_kinetic(ImageDraw.Draw(i1), "测试动画", 10, 10, fnt, WHITE, 0.05, 0.0)
        draw_row_kinetic(ImageDraw.Draw(i2), "测试动画", 10, 10, fnt, WHITE, 0.12, 0.0)
        assert np.abs(np.asarray(i1, np.int16) - np.asarray(i2, np.int16)).sum() > 0, \
            "kinetic text frozen"
        # character-free anime room: moving particles and node web must remain animated
        a1 = np.asarray(anime_still(0, 1.0, W2, H2), np.int16)
        a2 = np.asarray(anime_still(0, 1.6, W2, H2), np.int16)
        assert np.abs(a1 - a2).sum() > 0, "character-free scene frozen"
        out = os.path.join(tempfile.gettempdir(), "forge_selfcheck.mp4")
        r = forge(SELF_CHECK_SPEC, out)
        assert r["status"] == "ok" and r["bytes"] > 20000, r
        assert 3.0 < r["duration"] < 40.0, r
        print(json.dumps(r, ensure_ascii=False))
        sys.exit(0)
    if not a.spec:
        ap.error("--spec or --self-check required")
    spec = json.load(open(a.spec, encoding="utf-8")) if os.path.exists(a.spec) else json.loads(a.spec)
    out = a.out or spec.get("out") or os.path.join(os.getcwd(), "ego_video.mp4")
    print(json.dumps(forge(spec, out), ensure_ascii=False))
