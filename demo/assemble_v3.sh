#!/bin/bash
# Final video assembly v3: dynamic scene durations based on narration length
set -e

CLIPS_DIR="demo/clips"
SCREENS_DIR="demo/video/public/clips"
NARR_DIR="demo/audio/v3/processed"
MUSIC="demo/music/v2_2.mp3"
OUTPUT="demo/final_demo.mp4"
TMPDIR="/tmp/video_v3_$$"
mkdir -p "$TMPDIR"

echo "=== Final Demo Assembly v3 ==="

get_dur() {
    ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null | cut -d. -f1
}

# Scene definitions: type:source:narration_num
# Each scene duration = max(5s, narration_duration + 1s)
SCENES=(
    "title:THE PROBLEM:00"
    "clip:scene_01_v1_00001.mp4:01"
    "clip:scene_02_v1_00001.mp4:02"
    "clip:scene_03_v1_00001.mp4:03"
    "clip:scene_04_v1_00001.mp4:04"
    "title:ARCHITECTURE:00"
    "clip:scene_05_v1_00001.mp4:05"
    "clip:scene_06_v1_00001.mp4:06"
    "screen:screen_architecture.png:07"
    "clip:scene_08_v1_00001.mp4:08"
    "clip:scene_09_v1_00001.mp4:09"
    "clip:scene_10_v1_00001.mp4:10"
    "title:LIVE DEMO:00"
    "clip:scene_11_v1_00001.mp4:11"
    "screen:screen_terminal_launch.png:12"
    "screen:screen_discover.png:13"
    "clip:scene_14_v1_00001.mp4:00"
    "screen:screen_plan.png:15"
    "screen:screen_execute.png:16"
    "clip:scene_17_v1_00001.mp4:00"
    "screen:screen_verify.png:18"
    "title:INTEGRATIONS:00"
    "clip:scene_19_v1_00001.mp4:19"
    "screen:screen_agent_log.png:20"
    "clip:scene_21_v1_00001.mp4:00"
    "screen:screen_erc8004.png:22"
    "clip:scene_23_v1_00001.mp4:00"
    "clip:scene_24_v1_00001.mp4:24"
    "title:SAFETY:00"
    "clip:scene_25_v1_00001.mp4:25"
    "screen:screen_safety_1_4.png:26"
    "screen:screen_safety_5_8.png:27"
    "clip:scene_28_v1_00001.mp4:28"
    "screen:screen_guardrails.png:29"
    "clip:scene_30_v1_00001.mp4:00"
    "title:IMPACT:00"
    "clip:scene_31_v1_00001.mp4:31"
    "clip:scene_32_v1_00001.mp4:32"
    "clip:scene_33_v1_00001.mp4:33"
    "clip:scene_34_v1_00001.mp4:34"
    "clip:scene_35_v1_00001.mp4:35"
    "clip:scene_36_v1_00001.mp4:00"
    "closing:::00"
)

echo "Step 1: Building segments with dynamic durations..."
SEG=0
CONCAT="$TMPDIR/concat.txt"
> "$CONCAT"

for entry in "${SCENES[@]}"; do
    IFS=':' read -r type file extra narr <<< "$entry"
    SEG=$((SEG + 1))
    SEGFILE="$TMPDIR/seg_$(printf '%03d' $SEG).mp4"

    # Determine duration based on narration
    NARR_FILE="$NARR_DIR/narration_$narr.mp3"
    if [ -f "$NARR_FILE" ] && [ "$narr" != "00" ]; then
        NDUR=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$NARR_FILE" 2>/dev/null)
        # Scene duration = narration + 1.5s padding
        DUR=$(echo "$NDUR + 1.5" | bc)
        # Minimum 5 seconds
        if (( $(echo "$DUR < 5" | bc -l) )); then DUR=5; fi
    else
        DUR=5
    fi

    if [ "$type" = "title" ]; then
        if [ -z "$file" ] || [ "$file" = "" ]; then
            # Closing card
            ffmpeg -y -f lavfi -i "color=c=0x0a0e17:s=1280x720:d=5:r=24" \
                -vf "drawtext=text='SOVEREIGN MARKET INTELLIGENCE AGENT':fontcolor=0x4fc3f7:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2-30:font=monospace,drawtext=text='Let the agent cook.':fontcolor=0xfbbf24:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2+30:font=monospace" \
                -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p "$SEGFILE" 2>/dev/null
        else
            ffmpeg -y -f lavfi -i "color=c=0x0a0e17:s=1280x720:d=1.5:r=24" \
                -vf "drawtext=text='$file':fontcolor=0x4fc3f7:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2:font=monospace" \
                -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p "$SEGFILE" 2>/dev/null
        fi
    elif [ "$type" = "closing" ]; then
        ffmpeg -y -f lavfi -i "color=c=0x0a0e17:s=1280x720:d=5:r=24" \
            -vf "drawtext=text='SOVEREIGN MARKET INTELLIGENCE AGENT':fontcolor=0x4fc3f7:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2-30:font=monospace,drawtext=text='Let the agent cook.':fontcolor=0xfbbf24:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2+30:font=monospace" \
            -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p "$SEGFILE" 2>/dev/null
    elif [ "$type" = "screen" ]; then
        SRC="$SCREENS_DIR/$file"
        [ ! -f "$SRC" ] && continue
        ffmpeg -y -loop 1 -i "$SRC" -vf "scale=1280:720,fps=24" \
            -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p -t "$DUR" "$SEGFILE" 2>/dev/null
    else
        SRC="$CLIPS_DIR/$file"
        [ ! -f "$SRC" ] && continue
        ffmpeg -y -stream_loop -1 -i "$SRC" \
            -vf "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:-1:-1:color=0a0e17,fps=24" \
            -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p -t "$DUR" "$SEGFILE" 2>/dev/null
    fi

    echo "file '$SEGFILE'" >> "$CONCAT"
    printf "  [%03d] %ss %-8s %s\n" "$SEG" "$DUR" "$type" "${file:-title}"
done

echo ""
echo "Step 2: Concatenating..."
ffmpeg -y -f concat -safe 0 -i "$CONCAT" -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
    "$TMPDIR/video_only.mp4" 2>/dev/null

VDUR=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$TMPDIR/video_only.mp4")
echo "  Video: ${VDUR}s"

echo "Step 3: Building narration track..."
# Use Python to build timed narration matching the video
python3 -c "
import subprocess
from pathlib import Path

NARR_DIR = Path('$NARR_DIR')
VIDEO_DUR = float('$VDUR')
GAP = 0.5

sections = [
    [1,2,3,4],[5,6,7,8,9,10],[11,12,13,0,15,16,0,18],
    [19,20,0,22,0,24],[25,26,27,28,29,0],[31,32,33,34,35,0]
]

def get_dur(fp):
    r=subprocess.run(['ffprobe','-v','quiet','-show_entries','format=duration','-of','csv=p=0',str(fp)],capture_output=True,text=True)
    return float(r.stdout.strip())

t = 3.5
entries = []
for sec in sections:
    t += 1.5
    for sn in sec:
        f = NARR_DIR / f'narration_{sn:02d}.mp3' if sn > 0 else None
        if f and f.exists():
            dur = get_dur(f)
            entries.append((t, str(f)))
            t += max(dur + GAP, 5.0)
        else:
            t += 5.0

inputs = ['-f','lavfi','-i',f'anullsrc=r=44100:cl=mono:d={VIDEO_DUR}']
filters = []
used = 0
for i,(start,nf) in enumerate(entries):
    if start >= VIDEO_DUR - 2: break
    inputs.extend(['-i',nf])
    used += 1
    ms = int(start*1000)
    filters.append(f'[{used}:a]adelay={ms}|{ms}[n{used}]')

mix = '[0:a]'+''.join(f'[n{j}]' for j in range(1,used+1))
filters.append(f'{mix}amix=inputs={used+1}:duration=first:normalize=0[out]')

cmd = ['ffmpeg','-y']+inputs+['-filter_complex',';'.join(filters),'-map','[out]','-c:a','pcm_s16le','$TMPDIR/narration.wav']
subprocess.run(cmd, capture_output=True, text=True, timeout=120)
print(f'Narration: {used} clips placed')
"

echo "Step 4: Mixing narration + music..."
ffmpeg -y \
    -i "$TMPDIR/video_only.mp4" \
    -i "$TMPDIR/narration.wav" \
    -stream_loop -1 -i "$MUSIC" \
    -filter_complex "[1:a]volume=1.0[narr];[2:a]volume=0.12,afade=t=in:st=0:d=3,afade=t=out:st=$(echo "$VDUR - 3" | bc):d=3[music];[narr][music]amix=inputs=2:duration=first:normalize=0[aout]" \
    -map 0:v -map "[aout]" \
    -c:v copy -c:a aac -b:a 192k \
    -shortest \
    "$OUTPUT" 2>/dev/null

SIZE=$(du -h "$OUTPUT" | cut -f1)
FDUR=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$OUTPUT" | cut -d. -f1)
echo ""
echo "╔══════════════════════════════════════╗"
echo "║        FINAL DEMO COMPLETE           ║"
echo "║  $OUTPUT"
echo "║  Size: $SIZE | Length: ${FDUR}s"
echo "╚══════════════════════════════════════╝"

rm -rf "$TMPDIR"
