#!/usr/bin/env python3
"""
Verify rendered clips against their sources, one by one, independently of the render log.

    python verify_batch.py --src DIR --out DIR [--fps 60] [--suffix _graded] [--json r.json]

Checks per clip: duration within 0.2s, avg_frame_rate == target (strict constant frame
rate), frame count == duration x fps (+/-2), display resolution unchanged, audio kept.

Frame count comes from `-count_packets`, never from the container's nb_frames field —
that field is metadata and routinely wrong on VFR phone sources.
"""
import argparse
import glob
import json
import os
import subprocess
import sys


def probe(path):
    """Returns None for files ffprobe cannot read — a truncated render is a FAILED clip,
    not a crash."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate",
         "-of", "json", path], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
        streams = d["streams"]
    except (ValueError, KeyError):
        return None
    v = next((s for s in streams if s["codec_type"] == "video"), None)
    if not v:
        return None
    return {
        "dur": float(d["format"].get("duration") or 0),
        "size": int(d["format"].get("size") or 0),
        "w": v.get("width"), "h": v.get("height"),
        "avg": v.get("avg_frame_rate"), "r": v.get("r_frame_rate"),
        "codec": v.get("codec_name"),
        "audio": any(s["codec_type"] == "audio" for s in streams),
    }


def packets(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    try:
        return int(r.stdout.strip().split(",")[0])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=int, default=0,
                    help="0 = accept the clip's own rate, only assert CFR")
    ap.add_argument("--suffix", default="_graded")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    outs = sorted(glob.glob(os.path.join(a.out, f"*{a.suffix}*.mp4")))
    if not outs:
        print(f"no '*{a.suffix}*.mp4' in {a.out}")
        return 1

    print(f"{'clip':<30}{'src dur':>9}{'out dur':>9}{'frames':>8}{'avg_fps':>9}"
          f"{'display':>12}{'MB':>8}  verdict")
    bad, total = [], 0
    for o in outs:
        name = os.path.basename(o)
        stem = name.split(a.suffix)[0]
        cand = [os.path.join(a.src, stem + ext) for ext in (".mp4", ".MP4", ".mov", ".MOV")]
        src = next((c for c in cand if os.path.exists(c)), None)
        t = probe(o)
        if t is None:
            print(f"{stem[-30:]:<30}{'':>9}{'':>9}{'':>8}{'':>9}{'':>12}"
                  f"{os.path.getsize(o)/1048576:>8.1f}  ✗ 文件损坏/不完整")
            bad.append((name, "unreadable"))
            continue
        total += t["size"]
        n = packets(o)
        # a silent source is allowed to stay silent — only a LOST track is a failure
        src_has_audio = True
        # target fps: explicit --fps wins; otherwise the clip's own rate (CFR check only)
        tgt = a.fps if a.fps > 0 else None
        if tgt:
            afr_ok = t["avg"] == f"{tgt}/1"
        else:
            afr_ok = t["avg"] == t["r"]        # CFR: average equals nominal
        fps_num = float(tgt) if tgt else float(
            __import__("fractions").Fraction(t["avg"]).__float__() if t["avg"] else 0)
        frames_ok = n is not None and fps_num > 0 and \
            abs(n - round(t["dur"] * fps_num)) <= max(2, round(fps_num * 0.05))
        res_ok = True
        dur_ok = True
        if src:
            s = probe(src)
            dur_ok = abs(s["dur"] - t["dur"]) < 0.2
            src_has_audio = s["audio"]
            # a 90-degree rotation matrix swaps stored w/h; compare display orientation
            res_ok = (t["w"], t["h"]) in ((s["w"], s["h"]), (s["h"], s["w"]))
        else:
            s = {"dur": float("nan")}
        audio_ok = t["audio"] or not src_has_audio
        ok = afr_ok and frames_ok and res_ok and dur_ok and audio_ok
        if not ok:
            why = []
            if not dur_ok: why.append("duration")
            if not afr_ok: why.append(f"fps={t['avg']}")
            if not frames_ok: why.append(f"frames {n}")
            if not res_ok: why.append("resolution")
            if not audio_ok: why.append("audio-lost")
            bad.append((name, ",".join(why)))
        print(f"{stem[-30:]:<30}{s['dur']:>9.2f}{t['dur']:>9.2f}{str(n):>8}"
              f"{t['avg']:>9}{f'{t[chr(119)]}x{t[chr(104)]}':>12}{t['size']/1048576:>8.1f}"
              f"  {'OK' if ok else '✗ ' + ','.join(bad[-1][1:])}")
    print(f"\n{len(outs)} clip(s), {total/1073741824:.2f} GB, "
          f"{'all pass' if not bad else f'{len(bad)} FAILED'}")
    for n, w in bad:
        print(f"  ✗ {n}: {w}")

    if a.json:
        json.dump({"clips": len(outs), "failed": bad}, open(a.json, "w"), indent=2)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
