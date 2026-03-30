#!/bin/bash
# Assemble complete demo video with narration + music
# Usage: ./assemble_final.sh <music_file> <output_name>
set -e

MUSIC="$1"
OUTPUT="$2"
CLIPS_DIR="demo/clips"
SCREENS_DIR="demo/video/public/clips"
NARRATION_DIR="demo/audio/processed"
TMPDIR="/tmp/video_final_$$"
mkdir -p "$TMPDIR"

if [ -z "$MUSIC" ] || [ -z "$OUTPUT" ]; then
    echo "Usage: $0 <music.mp3> <output.mp4>"
    exit 1
fi

echo "=== Final Assembly: $OUTPUT ==="
echo "  Music: $MUSIC"

# Scene sequence: (type, file, narration_num)
# type: clip (AI video) or screen (PNG shown for 5s)
declare -a SCENES=(
    # Section 1: THE PROBLEM
    "title:THE PROBLEM"
    "clip:scene_01_v1_00001.mp4:01"
    "clip:scene_02_v1_00001.mp4:02"
    "clip:scene_03_v1_00001.mp4:03"
    "clip:scene_04_v1_00001.mp4:04"
    # Section 2: ARCHITECTURE
    "title:ARCHITECTURE"
    "clip:scene_05_v1_00001.mp4:05"
    "clip:scene_06_v1_00001.mp4:06"
    "screen:screen_architecture.png:07"
    "clip:scene_08_v1_00001.mp4:08"
    "clip:scene_09_v1_00001.mp4:09"
    "clip:scene_10_v1_00001.mp4:10"
    # Section 3: LIVE DEMO
    "title:LIVE DEMO"
    "clip:scene_11_v1_00001.mp4:11"
    "screen:screen_terminal_launch.png:12"
    "screen:screen_discover.png:13"
    "clip:scene_14_v1_00001.mp4:00"
    "screen:screen_plan.png:15"
    "screen:screen_execute.png:16"
    "clip:scene_17_v1_00001.mp4:00"
    "screen:screen_verify.png:18"
    # Section 4: INTEGRATIONS
    "title:INTEGRATIONS"
    "clip:scene_19_v1_00001.mp4:19"
    "screen:screen_agent_log.png:20"
    "clip:scene_21_v1_00001.mp4:00"
    "screen:screen_erc8004.png:22"
    "clip:scene_23_v1_00001.mp4:00"
    "clip:scene_24_v1_00001.mp4:24"
    # Section 5: SAFETY
    "title:SAFETY"
    "clip:scene_25_v1_00001.mp4:25"
    "screen:screen_safety_1_4.png:26"
    "screen:screen_safety_5_8.png:27"
    "clip:scene_28_v1_00001.mp4:28"
    "screen:screen_guardrails.png:29"
    "clip:scene_30_v1_00001.mp4:00"
    # Section 6: IMPACT
    "title:IMPACT"
    "clip:scene_31_v1_00001.mp4:31"
    "clip:scene_32_v1_00001.mp4:32"
    "clip:scene_33_v1_00001.mp4:33"
    "clip:scene_34_v1_00001.mp4:34"
    "clip:scene_35_v1_00001.mp4:35"
    "clip:scene_36_v1_00001.mp4:00"
    "title:closing"
)

# Step 1: Build each segment with narration
echo "Step 1: Building segments..."
SEG=0
CONCAT="$TMPDIR/concat.txt"
> "$CONCAT"

for entry in "${SCENES[@]}"; do
    IFS=':' read -r type file narr <<< "$entry"
    SEG=$((SEG + 1))
    SEGFILE="$TMPDIR/seg_$(printf '%03d' $SEG).mp4"

    if [ "$type" = "title" ]; then
        if [ "$file" = "closing" ]; then
            # Closing card - 5 seconds
            ffmpeg -y -f lavfi -i "color=c=0x0a0e17:s=1280x720:d=5:r=24" \
                -vf "drawtext=text='SOVEREIGN MARKET INTELLIGENCE AGENT':fontcolor=0x4fc3f7:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2-30:font=monospace,drawtext=text='Let the agent cook.':fontcolor=0xfbbf24:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2+30:font=monospace" \
                -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
                "$SEGFILE" 2>/dev/null
        else
            # Section title - 1.5 seconds
            ffmpeg -y -f lavfi -i "color=c=0x0a0e17:s=1280x720:d=1.5:r=24" \
                -vf "drawtext=text='$file':fontcolor=0x4fc3f7:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2:font=monospace" \
                -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
                "$SEGFILE" 2>/dev/null
        fi
        echo "file '$SEGFILE'" >> "$CONCAT"
        continue
    fi

    # Get source
    if [ "$type" = "clip" ]; then
        SRC="$CLIPS_DIR/$file"
    else
        SRC="$SCREENS_DIR/$file"
    fi

    if [ ! -f "$SRC" ]; then
        echo "  SKIP (missing): $SRC"
        continue
    fi

    # Check for narration
    NARR_FILE="$NARRATION_DIR/narration_$narr.mp3"

    if [ "$type" = "screen" ]; then
        # Convert image to 5-second video
        if [ -f "$NARR_FILE" ] && [ "$narr" != "00" ]; then
            # Get narration duration, use that for the screen
            DUR=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$NARR_FILE" | cut -d. -f1)
            DUR=$((DUR + 1))  # Add 1 second padding
            [ "$DUR" -lt 5 ] && DUR=5
            ffmpeg -y -loop 1 -i "$SRC" -i "$NARR_FILE" \
                -vf "scale=1280:720,fps=24" \
                -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
                -c:a aac -b:a 128k \
                -t "$DUR" -shortest \
                "$SEGFILE" 2>/dev/null
        else
            ffmpeg -y -loop 1 -i "$SRC" \
                -vf "scale=1280:720,fps=24" \
                -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
                -t 5 \
                "$SEGFILE" 2>/dev/null
        fi
    else
        # Video clip - normalize and optionally add narration
        if [ -f "$NARR_FILE" ] && [ "$narr" != "00" ]; then
            ffmpeg -y -i "$SRC" -i "$NARR_FILE" \
                -vf "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:-1:-1:color=0a0e17,fps=24" \
                -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
                -c:a aac -b:a 128k \
                -t 5 -shortest \
                "$SEGFILE" 2>/dev/null
        else
            ffmpeg -y -i "$SRC" \
                -vf "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:-1:-1:color=0a0e17,fps=24" \
                -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
                -an -t 5 \
                "$SEGFILE" 2>/dev/null
        fi
    fi

    echo "file '$SEGFILE'" >> "$CONCAT"
    printf "  [%03d] %-8s %-35s narr=%s\n" "$SEG" "$type" "$file" "$narr"
done

echo ""
echo "Step 2: Concatenating segments..."

# Concat all segments (some have audio, some don't)
ffmpeg -y -f concat -safe 0 -i "$CONCAT" \
    -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
    -c:a aac -b:a 128k \
    "$TMPDIR/video_with_narration.mp4" 2>/dev/null

echo "Step 3: Mixing in background music..."

# Get video duration
VDUR=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$TMPDIR/video_with_narration.mp4")
echo "  Video duration: ${VDUR}s"

# Loop music to match video length, mix at lower volume under narration
ffmpeg -y \
    -i "$TMPDIR/video_with_narration.mp4" \
    -stream_loop -1 -i "$MUSIC" \
    -filter_complex "[1:a]volume=0.15,afade=t=in:st=0:d=3,afade=t=out:st=$(echo "$VDUR - 3" | bc):d=3[music];[0:a][music]amix=inputs=2:duration=first:dropout_transition=3[aout]" \
    -map 0:v -map "[aout]" \
    -c:v copy -c:a aac -b:a 192k \
    -t "$VDUR" \
    "$OUTPUT" 2>/dev/null

if [ -f "$OUTPUT" ]; then
    SIZE=$(du -h "$OUTPUT" | cut -f1)
    FDUR=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$OUTPUT" | cut -d. -f1)
    echo ""
    echo "╔══════════════════════════════════════╗"
    echo "║        FINAL VIDEO COMPLETE          ║"
    echo "║  Output: $OUTPUT"
    echo "║  Size:   $SIZE"
    echo "║  Length: ${FDUR}s"
    echo "╚══════════════════════════════════════╝"
else
    echo "ERROR: Final output not created"
    # Fallback: output without music
    cp "$TMPDIR/video_with_narration.mp4" "$OUTPUT"
    echo "Fallback: saved without music mixing"
fi

rm -rf "$TMPDIR"
