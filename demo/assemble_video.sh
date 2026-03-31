#!/bin/bash
# Assemble the 3-minute demo video using ffmpeg
# No Remotion, no browser — pure ffmpeg concat + overlays
set -e

CLIPS_DIR="demo/clips"
OUTPUT="demo/sovereign_agent_demo.mp4"
TMPDIR="/tmp/video_assembly"
mkdir -p "$TMPDIR"

echo "=== Sovereign — Video Assembly ==="
echo ""

# Build the sequence file
# Each clip plays for its duration, section titles are 1-second black frames with text
cat > "$TMPDIR/sequence.txt" << 'SEQEOF'
# Section 1: THE PROBLEM (20s)
scene_01_v1_00001.mp4
scene_02_v1_00001.mp4
scene_03_v1_00001.mp4
scene_04_v1_00001.mp4
# Section 2: ARCHITECTURE (30s)
scene_05_v1_00001.mp4
scene_06_v1_00001.mp4
scene_08_v1_00001.mp4
scene_09_v1_00001.mp4
scene_10_v1_00001.mp4
scene_11_v1_00001.mp4
# Section 3: LIVE DEMO (40s — AI clips only, screen captures TBD)
scene_14_v1_00001.mp4
scene_17_v1_00001.mp4
# Section 4: INTEGRATIONS (30s)
scene_19_v1_00001.mp4
scene_21_v1_00001.mp4
scene_23_v1_00001.mp4
scene_24_v1_00001.mp4
# Section 5: SAFETY (30s)
scene_25_v1_00001.mp4
scene_28_v1_00001.mp4
scene_30_v1_00001.mp4
# Section 6: IMPACT (30s)
scene_31_v1_00001.mp4
scene_32_v1_00001.mp4
scene_33_v1_00001.mp4
scene_34_v1_00001.mp4
scene_35_v1_00001.mp4
scene_36_v1_00001.mp4
SEQEOF

# Step 1: Re-encode all clips to consistent format (same fps, resolution, codec)
echo "Step 1: Normalizing clips..."
COUNT=0
CONCAT_FILE="$TMPDIR/concat.txt"
> "$CONCAT_FILE"

while IFS= read -r line; do
    # Skip comments and blanks
    [[ "$line" =~ ^#.*$ ]] && continue
    [[ -z "$line" ]] && continue

    CLIP="$CLIPS_DIR/$line"
    if [ ! -f "$CLIP" ]; then
        echo "  SKIP (missing): $line"
        continue
    fi

    COUNT=$((COUNT + 1))
    NORMALIZED="$TMPDIR/clip_$(printf '%03d' $COUNT).mp4"

    ffmpeg -y -i "$CLIP" \
        -vf "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:-1:-1:color=0a0e17,fps=24" \
        -c:v libx264 -preset fast -crf 20 \
        -an -t 5 \
        "$NORMALIZED" 2>/dev/null

    echo "file '$NORMALIZED'" >> "$CONCAT_FILE"
    echo "  [$COUNT] $line → normalized"

done < "$TMPDIR/sequence.txt"

echo ""
echo "Step 2: Generating title cards..."

# Generate section title cards (1 second each, dark bg with cyan text)
SECTIONS=("THE PROBLEM" "ARCHITECTURE" "LIVE DEMO" "INTEGRATIONS" "SAFETY" "IMPACT")
for i in "${!SECTIONS[@]}"; do
    TITLE="${SECTIONS[$i]}"
    TITLE_FILE="$TMPDIR/title_$i.mp4"

    ffmpeg -y -f lavfi -i "color=c=0x0a0e17:s=1280x720:d=1.5:r=24" \
        -vf "drawtext=text='$TITLE':fontcolor=0x4fc3f7:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2:font=monospace" \
        -c:v libx264 -preset fast -crf 20 \
        "$TITLE_FILE" 2>/dev/null

    echo "  Title: $TITLE"
done

# Generate closing card
ffmpeg -y -f lavfi -i "color=c=0x0a0e17:s=1280x720:d=5:r=24" \
    -vf "drawtext=text='SOVEREIGN MARKET INTELLIGENCE AGENT':fontcolor=0x4fc3f7:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2-30:font=monospace,drawtext=text='Let the agent cook.':fontcolor=0xfbbf24:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2+30:font=monospace" \
    -c:v libx264 -preset fast -crf 20 \
    "$TMPDIR/closing.mp4" 2>/dev/null

echo "  Closing card"

echo ""
echo "Step 3: Building final concat list with titles..."

# Build final concat with titles interleaved
FINAL_CONCAT="$TMPDIR/final_concat.txt"
> "$FINAL_CONCAT"

CLIP_INDEX=0
SECTION_CLIPS=(4 6 2 4 3 6)  # clips per section

for s in "${!SECTIONS[@]}"; do
    # Section title
    echo "file '$TMPDIR/title_$s.mp4'" >> "$FINAL_CONCAT"

    # Section clips
    N=${SECTION_CLIPS[$s]}
    for ((c=0; c<N; c++)); do
        CLIP_INDEX=$((CLIP_INDEX + 1))
        CLIP_FILE="$TMPDIR/clip_$(printf '%03d' $CLIP_INDEX).mp4"
        if [ -f "$CLIP_FILE" ]; then
            echo "file '$CLIP_FILE'" >> "$FINAL_CONCAT"
        fi
    done
done

# Closing
echo "file '$TMPDIR/closing.mp4'" >> "$FINAL_CONCAT"

echo ""
echo "Step 4: Concatenating into final video..."

ffmpeg -y -f concat -safe 0 -i "$FINAL_CONCAT" \
    -c:v libx264 -preset medium -crf 18 \
    -movflags +faststart \
    "$OUTPUT" 2>/dev/null

if [ -f "$OUTPUT" ]; then
    SIZE=$(du -h "$OUTPUT" | cut -f1)
    DURATION=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$OUTPUT" | cut -d. -f1)
    echo ""
    echo "=== DONE ==="
    echo "Output: $OUTPUT"
    echo "Size: $SIZE"
    echo "Duration: ${DURATION}s"
    echo ""
else
    echo "ERROR: Output file not created"
fi
