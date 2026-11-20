#!/usr/bin/env python3
"""
Probe phone/camera footage BEFORE grading: HDR or SDR, CFR or VFR, rotated, frame count.

Usage:
    python probe_media.py FILE.mp4 [FILE2.mp4 ...] [--json out.json]

Why this exists: phone clips lie. `color_transfer` is often `unknown`, the real display
orientation lives in a rotation matrix, and the stream is usually VFR even though
r_frame_rate claims 60/1. All three need to be known before choosing an encode command.
"""
import argparse
import json
import subprocess
import sys
from collections import Counter
from fractions import Fraction


def ffprobe(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,"
         "pix_fmt,r_frame_rate,avg_frame_rate,nb_frames,color_primaries,color_transfer,"
         "color_space,color_range:stream_side_data=rotation",
         "-of", "json", path],
        capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def packet_durations(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=duration_time", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    return [x for x in r.stdout.split() if x]


def packet_count(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    try:
        return int(r.stdout.strip().split(",")[0])
    except Exception:
        return None


def rate(s):
    try:
        f = Fraction(s)
        return float(f) if f.denominator else 0.0
    except Exception:
        return 0.0


def analyse(path):
    d = ffprobe(path)
    if not d:
        return {"file": path, "error": "ffprobe failed"}
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return {"file": path, "error": "no video stream"}
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = d.get("format", {})

    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    for sd in v.get("side_data", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))

    dur = float(fmt.get("duration") or 0)
    fps_r = rate(v.get("r_frame_rate", "0/1"))
    fps_avg = rate(v.get("avg_frame_rate", "0/1"))
    nf = v.get("nb_frames")
    nf = int(nf) if nf and str(nf).isdigit() else None

    # VFR verdict from the actual packet durations, not the container field
    pd = packet_durations(path)
    is_vfr = False
    odd = 0
    if pd:
        c = Counter(pd)
        main, main_n = c.most_common(1)[0]
        odd = len(pd) - main_n
        is_vfr = odd > 0

    transfer = (v.get("color_transfer") or "").lower()
    prim = (v.get("color_primaries") or "").lower()
    bits = 10 if "10le" in (v.get("pix_fmt") or "") or "10be" in (v.get("pix_fmt") or "") else 8

    hdr = None
    if transfer in ("smpte2084", "smpte-st-2084"):
        hdr = "PQ"
    elif transfer in ("arib-std-b67", "arib-std-b67"):
        hdr = "HLG"
    elif transfer in ("", "unknown", "reserved") and prim == "bt2020" and bits == 10:
        hdr = "PQ?"   # very common on phones: metadata stripped, content is PQ

    w, h = v.get("width"), v.get("height")
    if abs(rot) == 90:
        w, h = h, w

    return {
        "file": path,
        "codec": v.get("codec_name"),
        "pix_fmt": v.get("pix_fmt"),
        "bits": bits,
        "storage": f"{v.get('width')}x{v.get('height')}",
        "display": f"{w}x{h}",
        "rotation": rot,
        "duration": round(dur, 3),
        "fps_r": round(fps_r, 3),
        "fps_avg": round(fps_avg, 3),
        "vfr": is_vfr,
        "odd_frames": odd,
        "nb_frames_meta": nf,
        "packets": packet_count(path),
        "audio": bool(a),
        "primaries": prim or "?",
        "transfer": transfer or "?",
        "hdr": hdr,
        "size_mb": round(int(fmt.get("size") or 0) / 1048576, 1),
        "needs_rotate": abs(rot) == 90,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    rows = [analyse(f) for f in args.files]
    ok = [r for r in rows if "error" not in r]

    hdr = "-" * 100
    print(hdr)
    print(f"{'file':<34}{'dur':>7}{'fps_r':>7}{'fps_avg':>9}{'VFR':>5}"
          f"{'display':>12}{'bits':>5}{'transfer':>10}{'HDR':>6}{'MB':>8}")
    print(hdr)
    for r in rows:
        if "error" in r:
            print(f"{r['file'][-34:]:<34}  ERROR: {r['error']}")
            continue
        name = r["file"].replace("\\", "/").split("/")[-1][-34:]
        print(f"{name:<34}{r['duration']:>7.2f}{r['fps_r']:>7.1f}{r['fps_avg']:>9.2f}"
              f"{('YES' if r['vfr'] else 'no'):>5}{r['display']:>12}"
              f"{r['bits']:>5}{r['transfer']:>10}{str(r['hdr']):>6}{r['size_mb']:>8.1f}")
    print(hdr)

    # ---- what this means for the encode command ----
    if ok:
        hdrs = {r["hdr"] for r in ok}
        vfrs = [r for r in ok if r["vfr"]]
        rots = [r for r in ok if r["needs_rotate"]]
        print("\n结论 / actions:")
        if any(h and h.startswith("PQ") for h in hdrs):
            print("  • HDR(PQ) detected -> tone-map to Rec.709 FIRST "
                  "(zscale tin=smpte2084 ... see SKILL.md), never grade in PQ space.")
            if any(r["transfer"] in ("?", "unknown") for r in ok):
                print("  • transfer=unknown: the tag is missing, so it MUST be forced with "
                      "`zscale=tin=smpte2084`. Without it the image decodes as SDR and looks "
                      "washed out / grey.")
        elif "HLG" in hdrs:
            print("  • HDR(HLG) -> same tone-map path but `tin=arib-std-b67`.")
        else:
            print("  • SDR Rec.709 -> go straight to the corrective base.")

        if vfrs:
            fps = max(r["fps_avg"] for r in vfrs)
            print(f"  • {len(vfrs)}/{len(ok)} clips are VFR (avg ≈ {fps:.2f}). "
                  f"ALWAYS encode with `-r {round(fps)} -fps_mode cfr`.")
            print("    Editing apps (剪映/CapCut, Premiere) normalise VFR and often report "
                  "it as 30fps — the file is fine, the timeline is not.")
        if rots:
            print(f"  • {len(rots)} clip(s) carry a rotation matrix: pixels are stored "
                  "sideways, ffmpeg auto-rotates. Do NOT add -vf transpose.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"\nJSON -> {args.json}")


if __name__ == "__main__":
    sys.exit(main())
