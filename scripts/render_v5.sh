#!/bin/bash
# V5 pipeline — the proven chain: HDR tone map -> look LUT -> clarity -> Pro-Mist glow
# -> vignette -> strict CFR. This is the combination that produced the accepted delivery.
#
#   SRC_DIR=/path/in OUT_DIR=/path/out bash render_v5.sh
#
# Vars (defaults in brackets):
#   SRC_DIR OUT_DIR   source / output folders                [required]
#   LUT               .cube look, default the bundled v3 LUT  [assets/luts/selected-color-look-v3.cube]
#   HDR               pq | hlg | sdr | log                    [pq]
#   PRE_LUT           .cube applied BEFORE the look LUT       []
#   POST_LUT          .cube applied AFTER the look LUT        []
#                     use for shot matching across a set
#                     use for Log footage: bake a decode LUT with make_curve_lut.py
#                     and pass it here; the HDR tone map is then skipped
#   FPS               output constant frame rate              [60]
#   CRF / PRESET      x265 quality / speed                    [19 / veryfast]
#   CLARITY           unsharp amount, 0 disables              [0.30]
#   GLOW              Pro-Mist strength, 0 disables           [0.37]
#   GLOW_SIGMA        blur radius for the glow                [10]
#   VIGNETTE          angle, empty disables                   [PI/12]
#   CLIPS             short ids, or "all"                     [all]
#   TEST_ONE          1 = shortest clip only, then stop       [0]
#   MARK_DIR          resume markers / logs                   [./run]
set -u

SRC_DIR="${SRC_DIR:?set SRC_DIR}"
OUT_DIR="${OUT_DIR:?set OUT_DIR}"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LUT="${LUT:-$SKILL_DIR/assets/luts/selected-color-look-v3.cube}"
HDR="${HDR:-pq}"
FPS="${FPS:-60}"
CRF="${CRF:-19}"
PRESET="${PRESET:-veryfast}"
CLARITY="${CLARITY:-0.30}"
GLOW="${GLOW:-0.37}"
GLOW_SIGMA="${GLOW_SIGMA:-10}"
VIGNETTE="${VIGNETTE:-PI/12}"
CLIPS="${CLIPS:-all}"
TEST_ONE="${TEST_ONE:-0}"

BASE="${MARK_DIR:-$(pwd)/run}"
MARK="$BASE/done"
LOG="$BASE/v5.log"
mkdir -p "$OUT_DIR" "$MARK"

# lut3d needs a bare filename: a Windows drive letter contains ':' which the filter-graph
# parser treats as an option separator.
LUT_DIR="$(cd "$(dirname "$LUT")" && pwd)"
LUT_NAME="$(basename "$LUT")"
cd "$LUT_DIR" || exit 1

case "$HDR" in
  pq)  TM="zscale=tin=smpte2084:min=bt2020nc:pin=bt2020:rin=full:t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p10le" ;;
  hlg) TM="zscale=tin=arib-std-b67:min=bt2020nc:pin=bt2020:rin=full:t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p10le" ;;
  sdr) TM="format=yuv420p10le" ;;
  log) TM="format=yuv420p10le" ;;   # PRE_LUT does the log->linear->709 conversion
  *)   echo "HDR must be pq|hlg|sdr|log"; exit 2 ;;
esac

# stage assembly: tone map -> (pre LUT) -> LUT -> clarity -> glow -> vignette
VF="$TM"
if [ -n "${PRE_LUT:-}" ]; then
  PRE_NAME="$(basename "$PRE_LUT")"
  cp -f "$PRE_LUT" "$LUT_DIR/$PRE_NAME" 2>/dev/null || true
  VF="$VF,lut3d=$PRE_NAME"
fi
VF="$VF,lut3d=$LUT_NAME"
# POST_LUT: shot-matching correction, applied after the look so the whole set agrees
if [ -n "${POST_LUT:-}" ]; then
  POST_NAME="$(basename "$POST_LUT")"
  cp -f "$POST_LUT" "$LUT_DIR/$POST_NAME" 2>/dev/null || true
  VF="$VF,lut3d=$POST_NAME"
fi
[ "$(python -c "print(1 if float('$CLARITY')>0 else 0)")" = "1" ] && \
  VF="$VF,unsharp=7:7:${CLARITY}:7:7:0"
if [ "$(python -c "print(1 if float('$GLOW')>0 else 0)")" = "1" ]; then
  # The glow MUST run in float RGB. On YUV, blend mixes the chroma planes and the frame
  # turns into coloured mush; `c1_mode=none` does not help. gbrp16le is NOT an acceptable
  # substitute either — it darkens the image by 33/255 and triples the bitrate.
  VF="$VF,format=gbrpf32le,split[m][t];[t]scale=iw/4:ih/4,gblur=sigma=${GLOW_SIGMA},scale=iw*4:ih*4,colorbalance=rm=0.10:gm=0.03:bm=-0.08[g];[m][g]blend=all_mode=softlight:all_opacity=${GLOW},format=yuv420p10le"
fi
[ -n "$VIGNETTE" ] && VF="$VF,vignette=angle=$VIGNETTE"

echo "[$(date +%H:%M:%S)] ===== V5 start hdr=$HDR fps=$FPS lut=$LUT_NAME clarity=$CLARITY glow=$GLOW =====" >> "$LOG"

resolve_src() {
  local id="$1"
  for c in "$SRC_DIR/$id.mp4" "$SRC_DIR/$id.MP4" "$SRC_DIR/$id.mov"; do
    [ -f "$c" ] && { echo "$c"; return; }
  done
  ls "$SRC_DIR"/*"$id"*.mp4 2>/dev/null | head -1
}

if [ "$CLIPS" = "all" ]; then
  CLIPS=$(cd "$SRC_DIR" && ls *.mp4 *.MP4 2>/dev/null | sed 's/\.[^.]*$//' | tr '\n' ' ')
fi

if [ "$TEST_ONE" = "1" ]; then
  s=""; sd=999999
  for id in $CLIPS; do
    p=$(resolve_src "$id"); [ -f "$p" ] || continue
    d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$p" 2>/dev/null); d=${d:-999999}
    python -c "import sys;sys.exit(0 if $d < $sd else 1)" && { s="$id"; sd="$d"; }
  done
  CLIPS="$s"; echo "[$(date +%H:%M:%S)] TEST_ONE: $s (${sd}s)" >> "$LOG"
fi

total=0; done_d=0; t_start=$(date +%s)
for id in $CLIPS; do p=$(resolve_src "$id"); [ -f "$p" ] || continue
  d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$p" 2>/dev/null)
  total=$(python -c "print($total + ${d:-0})"); done
echo "[$(date +%H:%M:%S)] workload ${total}s" >> "$LOG"

for id in $CLIPS; do
  src=$(resolve_src "$id"); [ -f "${src:-}" ] || { echo "[$(date +%H:%M:%S)] $id MISSING" >> "$LOG"; continue; }
  stem="$(basename "$src")"; stem="${stem%.*}"
  out="$OUT_DIR/${stem}_graded.mp4"
  if [ -f "$MARK/$stem" ] && [ -f "$out" ]; then echo "[$(date +%H:%M:%S)] $stem SKIP" >> "$LOG"; continue; fi

  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$src")
  t0=$(date +%s); echo "[$(date +%H:%M:%S)] $stem START ${dur}s" >> "$LOG"

  ffmpeg -hide_banner -loglevel error -y -i "$src" -filter_complex "[0:v]$VF[v]" \
    -map "[v]" -map 0:a? \
    -r "$FPS" -fps_mode cfr \
    -filter_threads 2 -threads 6 \
    -c:v libx265 -preset "$PRESET" -crf "$CRF" -x265-params log-level=error \
    -pix_fmt yuv420p10le -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
    -c:a copy -movflags +faststart "$out" >> "$LOG" 2>&1
  rc=$?; t1=$(date +%s)

  odur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$out" 2>/dev/null)
  afr=$(ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of csv=p=0 "$out" 2>/dev/null)
  st=$(python -c "print('ok' if abs(float('$odur' or 0)-float('$dur'))<0.2 and '$afr'=='$FPS/1' else 'CHECK')")
  echo "[$(date +%H:%M:%S)] $stem DONE rc=$rc $((t1-t0))s dur=${odur} fps=$afr $st" >> "$LOG"
  [ "$st" = "ok" ] && touch "$MARK/$stem"

  done_d=$(python -c "print($done_d + ${dur:-0})"); el=$((t1-t_start))
  python - "$done_d" "$total" "$el" <<'PY' | tee -a "$LOG"
import sys
d,t,e = float(sys.argv[1]),float(sys.argv[2]),float(sys.argv[3])
if d>0 and t>d: print(f"    进度 {d/t*100:.0f}%  速度 {e/d:.1f}x 实时  剩余约 {(t-d)*e/d/60:.0f} 分钟")
PY
done
echo "[$(date +%H:%M:%S)] ALL_DONE" >> "$LOG"
