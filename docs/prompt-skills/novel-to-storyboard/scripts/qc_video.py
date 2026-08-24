#!/usr/bin/env python3
"""
成片客观质检 — 分镜提示词工作流的第 7 步。

输出：规格参数、切点分布、每段色温/亮度/锐度、响度与静音、
      接触表 JPG、人脸比对 JPG。

用法：
    python qc_video.py video.mp4 --out ./qc --seg 15
    python qc_video.py a.mp4 b.mp4 --out ./qc --seg 15 10   # 两片对比

依赖：ffmpeg / ffprobe，Python: opencv-python, pillow, numpy
"""

import argparse
import json
import os
import re
import subprocess
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw

# 切点检测阈值。同色调段落（整段绿雾、整段夜戏）在高阈值下会漏检，
# 所以默认取低阈值并做去抖，宁可多报也不漏报。
SCENE_THRESHOLD = 0.12
DEDUP_WINDOW = 0.4  # 秒，该窗口内的多个切点合并为一个


def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def probe(path):
    """读取规格参数。"""
    q = ("ffprobe -v error -show_entries "
         "format=duration,bit_rate -show_entries "
         "stream=codec_type,codec_name,width,height,r_frame_rate "
         f'-of json "{path}"')
    r = run(q)
    d = json.loads(r.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
    fps = v.get("r_frame_rate", "0/1")
    try:
        num, den = fps.split("/")
        fps = round(float(num) / float(den), 2)
    except Exception:
        fps = None
    return {
        "width": v.get("width"), "height": v.get("height"),
        "fps": fps, "vcodec": v.get("codec_name"), "acodec": a.get("codec_name"),
        "duration": float(d.get("format", {}).get("duration", 0) or 0),
        "bitrate_mbps": round(int(d.get("format", {}).get("bit_rate", 0) or 0) / 1e6, 2),
    }


def cuts(path, threshold=SCENE_THRESHOLD):
    """检测切点。返回去抖后的时间列表。"""
    cmd = (f'ffmpeg -hide_banner -loglevel error -i "{path}" '
           f"-vf \"select='gt(scene,{threshold})',metadata=print:file=-\" "
           "-an -f null - 2>/dev/null")
    r = run(cmd)
    ts = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", r.stdout)]
    out = []
    for t in sorted(ts):
        if t < 0.5:            # 首帧不算切点
            continue
        if out and t - out[-1] < DEDUP_WINDOW:
            continue
        out.append(round(t, 1))
    return out


def loudness(path):
    """EBU R128 响度。"""
    r = run(f'ffmpeg -hide_banner -i "{path}" -filter:a ebur128=peak=true -f null - 2>&1')
    txt = r.stdout + r.stderr
    # ebur128 会把每一帧的瞬时读数都打出来，开头几帧是 -70 LUFS 的静音值。
    # 取最后一次匹配，也就是 Summary 里的最终积分值。
    def grab(pat):
        m = re.findall(pat, txt)
        return float(m[-1]) if m else None

    return {
        "integrated_lufs": grab(r"I:\s*(-?[\d.]+) LUFS"),
        "lra_lu": grab(r"LRA:\s*([\d.]+) LU"),
        "true_peak_dbfs": grab(r"Peak:\s*(-?[\d.]+) dBFS"),
    }


def silences(path, thresh_db=-45, min_dur=1.0):
    r = run(f'ffmpeg -hide_banner -i "{path}" '
            f"-af silencedetect=n={thresh_db}dB:d={min_dur} -f null - 2>&1")
    txt = r.stdout + r.stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", txt)]
    durs = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", txt)]
    return [{"start": round(s, 1), "duration": round(d, 1)}
            for s, d in zip(starts, durs)]


def segment_stats(path, seg_len, duration):
    """每段色温(R-B)、饱和、亮度、锐度。R-B > 8 判暖，< -8 判冷。"""
    cap = cv2.VideoCapture(path)
    rows = []
    n = max(1, int(np.ceil(duration / seg_len)))
    for i in range(n):
        a, b = i * seg_len, min((i + 1) * seg_len, duration)
        # 末尾不足半段的残段样本太少，统计不可靠，且会污染"中性段占比"，丢掉。
        if b - a < seg_len * 0.5:
            continue
        vals = []
        for t in np.linspace(a + 0.5, max(a + 0.6, b - 0.5), 4):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, fr = cap.read()
            if not ok:
                continue
            small = cv2.resize(fr, (320, 180) if fr.shape[1] > fr.shape[0] else (180, 320))
            rb = float(small[:, :, 2].mean() - small[:, :, 0].mean())
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            vals.append((rb, hsv[:, :, 1].mean(), hsv[:, :, 2].mean(),
                         cv2.Laplacian(gray, cv2.CV_64F).var()))
        if not vals:
            continue
        m = np.array(vals).mean(axis=0)
        rows.append({
            "seg": i + 1, "start": round(a, 1), "end": round(b, 1),
            "r_minus_b": round(float(m[0]), 1),
            "tone": "暖" if m[0] > 8 else ("冷" if m[0] < -8 else "中性"),
            "saturation": round(float(m[1]), 1),
            "brightness": round(float(m[2]), 1),
            "sharpness": round(float(m[3]), 1),
        })
    cap.release()
    return rows


def _grab(path, times):
    cap = cv2.VideoCapture(path)
    out = []
    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if ok:
            out.append((t, cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)))
    cap.release()
    return out


def _grid(images, cols, cell_w, outp):
    ims = []
    for t, arr in images:
        im = Image.fromarray(arr)
        im = im.resize((cell_w, int(im.height * cell_w / im.width)))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 62, 18], fill=(0, 0, 0))
        d.text((4, 4), f"{t:.1f}s", fill=(255, 255, 0))
        ims.append(im)
    if not ims:
        return None
    ch = max(i.height for i in ims)
    rows = (len(ims) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * cell_w, rows * ch), (20, 20, 20))
    for i, im in enumerate(ims):
        canvas.paste(im, ((i % cols) * cell_w, (i // cols) * ch))
    canvas.save(outp, quality=88)
    return outp


def contact_sheet(path, cut_list, duration, outp, cols=4):
    """按镜头中点抽帧做接触表——一眼看清每个镜头拍到了什么。"""
    bounds = [0.0] + cut_list + [duration]
    mids = [(bounds[i] + bounds[i + 1]) / 2 for i in range(len(bounds) - 1)]
    return _grid(_grab(path, mids), cols, 380, outp)


def face_sheet(path, cut_list, duration, outp, cell=200):
    """抽取每个镜头里最大的一张脸，横排比对跨镜一致性。"""
    casc = cv2.CascadeClassifier(cv2.data.haarcascades +
                                 "haarcascade_frontalface_default.xml")
    bounds = [0.0] + cut_list + [duration]
    mids = [(bounds[i] + bounds[i + 1]) / 2 for i in range(len(bounds) - 1)]
    cap = cv2.VideoCapture(path)
    crops = []
    for t in mids:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        found = casc.detectMultiScale(g, 1.1, 5, minSize=(60, 60))
        if len(found) == 0:
            continue
        x, y, w, h = max(found, key=lambda r: r[2] * r[3])
        pad = int(w * 0.35)
        crop = fr[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
        im = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)).resize((cell, cell))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 62, 18], fill=(0, 0, 0))
        d.text((4, 4), f"{t:.0f}s", fill=(255, 255, 0))
        crops.append(im)
    cap.release()
    if not crops:
        return None
    cols = min(len(crops), 8)
    rows = (len(crops) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * cell, rows * cell), (20, 20, 20))
    for i, im in enumerate(crops):
        canvas.paste(im, ((i % cols) * cell, (i // cols) * cell))
    canvas.save(outp, quality=90)
    return outp


def report(path, outdir, seg_len):
    name = os.path.splitext(os.path.basename(path))[0]
    spec = probe(path)
    c = cuts(path)
    dur = spec["duration"]
    segs = segment_stats(path, seg_len, dur)
    loud = loudness(path)
    sil = silences(path)
    sheet = contact_sheet(path, c, dur, os.path.join(outdir, f"{name}_接触表.jpg"))
    faces = face_sheet(path, c, dur, os.path.join(outdir, f"{name}_面部比对.jpg"))

    print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
    print(f"规格      {spec['width']}x{spec['height']}  {spec['fps']}fps  "
          f"{spec['bitrate_mbps']} Mbps  {dur:.1f}s  "
          f"{spec['vcodec']}/{spec['acodec']}")
    orient = "竖屏" if (spec['height'] or 0) > (spec['width'] or 0) else "横屏"
    min_side = min(spec['width'] or 0, spec['height'] or 0)
    flag = "  ← 低于 1080，手机满屏会发糊" if min_side < 1080 else ""
    print(f"          {orient}，短边 {min_side}{flag}")

    print(f"\n切点({len(c)}个，阈值{SCENE_THRESHOLD}) {c}")
    shots = len(c) + 1
    print(f"实际镜头数 {shots}   平均镜长 {dur / shots:.1f}s")
    if segs:
        print(f"一致性接缝数 {max(0, len(segs) - 1)}（段边界数；接缝越少越稳）")

    print("\n分段色调：")
    for s in segs:
        print(f"  段{s['seg']:02d} {s['start']:>6.1f}-{s['end']:<6.1f} "
              f"R-B={s['r_minus_b']:+6.1f} {s['tone']:<3s} "
              f"饱和={s['saturation']:5.1f} 亮度={s['brightness']:5.1f} "
              f"锐度={s['sharpness']:7.1f}")
    tones = [s["tone"] for s in segs]
    if tones.count("中性") > len(tones) / 3:
        print("  ⚠ 中性段过多，色调基调没立住（见 failure-modes.md D2）")

    print(f"\n响度      {loud['integrated_lufs']} LUFS  "
          f"LRA {loud['lra_lu']} LU  真峰 {loud['true_peak_dbfs']} dBFS")
    if loud["lra_lu"] and loud["lra_lu"] > 12:
        print("  ⚠ LRA 过宽，手机外放会听不见对白（目标 8-10 LU）")
    if loud["true_peak_dbfs"] and loud["true_peak_dbfs"] > -1.0:
        print("  ⚠ 真峰过高，平台转码有削波风险（目标 -1.5 dBTP）")
    if loud["integrated_lufs"] and loud["integrated_lufs"] < -16:
        print("  ⚠ 整体偏轻，短视频平台目标 -14 LUFS")
    if sil:
        print(f"  ⚠ 静音空档 {sil}")

    print(f"\n接触表    {sheet}")
    print(f"面部比对  {faces}")
    return {"name": name, "spec": spec, "cuts": c, "segments": segs,
            "loudness": loud, "silences": sil,
            "contact_sheet": sheet, "face_sheet": faces}


def main():
    ap = argparse.ArgumentParser(description="成片客观质检")
    ap.add_argument("videos", nargs="+", help="一个或多个视频文件")
    ap.add_argument("--out", default="./qc", help="输出目录")
    ap.add_argument("--seg", type=float, nargs="*", default=[15],
                    help="每片的段长（秒）。给一个值则所有片子共用")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    segs = args.seg if len(args.seg) == len(args.videos) else [args.seg[0]] * len(args.videos)

    results = []
    for path, seg in zip(args.videos, segs):
        if not os.path.exists(path):
            print(f"找不到文件：{path}", file=sys.stderr)
            continue
        results.append(report(path, args.out, seg))

    with open(os.path.join(args.out, "qc.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n完整数据：{os.path.join(args.out, 'qc.json')}")
    print("读数方法与验收门槛见 references/qc-checklist.md")


if __name__ == "__main__":
    main()
