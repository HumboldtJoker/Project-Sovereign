#!/bin/bash
# Assemble demo video v2 — uses all available AI clips
# Automatically discovers clips, generates titles, concatenates
set -e

CLIPS_DIR="demo/clips"
OUTPUT="demo/sovereign_agent_demo.mp4"
TMPDIR="/tmp/video_assembly_v2"
rm -rf "$TMPDIR"
mkdir -p "$TMPDIR"

echo "=== Sovereign Market Intelligence Agent — Video Assembly v2 ==="

# Map scenes to sections
declare -A SECTION_MAP
for s in 01 02 03 04; do SECTION_MAP[$s]="THE PROBLEM"; done
for s in 05 06 08 09 10 11; do SECTION_MAP[$s]="ARCHITECTURE"; done
for s in 14 17; do SECTION_MAP[$s]="LIVE DEMO"; done
for s in 19 21 23 24; do SECTION_MAP[$s]="INTEGRATIONS"; done
for s in 25 28 30; do SECTION_MAP[$s]="SAFETY"; done
for s in 31 32 33 34 35 36; do SECTION_MAP[$s]="IMPACT"; done

SECTIONS_ORDER=("THE PROBLEM" "ARCHITECTURE" "LIVE DEMO" "INTEGRATIONS" "SAFETY" "IMPACT")

echo ""
echo "Step 1: Discovering and normalizing clips..."
COUNT=0

# Collect clips per section
declare -A SECTION_CLIPS
for section in "${SECTIONS_ORDER[@]}"; do
    SECTION_CLIPS[$section]=""
done

# Sort clips and assign to sections
for clip in $(ls "$CLIPS_DIR"/scene_*_v1_*.mp4 2>/dev/null | sort); do
    fname=$(basename "$clip")
    # Extract scene number: scene_XX_v1_00001.mp4
    scene_num=$(echo "$fname" | sed 's/scene_\([0-9]*\)_.*/\1/')

    section="${SECTION_MAP[$scene_num]}"
    if [ -z "$section" ]; then
        echo "  SKIP (unmapped): $fname"
        continue
    fi

    COUNT=$((COUNT + 1))
    NORM="$TMPDIR/clip_$(printf '%03d' $COUNT).mp4"

    ffmpeg -y -i "$clip" \
        -vf "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:-1:-1:color=0a0e17,fps=24" \
        -c:v libx264 -preset fast -crf 20 \
        -an -t 5 \
        "$NORM" 2>/dev/null

    SECTION_CLIPS[$section]="${SECTION_CLIPS[$section]} $NORM"
    echo "  [$COUNT] $fname → $section"
done

echo ""
echo "  $COUNT clips normalized across ${#SECTIONS_ORDER[@]} sections"

echo ""
echo "Step 2: Generating title cards..."

for i in "${!SECTIONS_ORDER[@]}"; do
    TITLE="${SECTIONS_ORDER[$i]}"
    TITLE_FILE="$TMPDIR/title_$i.mp4"

    ffmpeg -y -f lavfi -i "color=c=0x0a0e17:s=1280x720:d=1.5:r=24" \
        -vf "drawtext=text='$TITLE':fontcolor=0x4fc3f7:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2:font=monospace" \
        -c:v libx264 -preset fast -crf 20 \
        "$TITLE_FILE" 2>/dev/null

    echo "  Title: $TITLE"
done

# Opening card
ffmpeg -y -f lavfi -i "color=c=0x0a0e17:s=1280x720:d=3:r=24" \
    -vf "drawtext=text='SOVEREIGN MARKET':fontcolor=0x4fc3f7:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2-40:font=monospace,drawtext=text='INTELLIGENCE AGENT':fontcolor=0x4fc3f7:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2+20:font=monospace,drawtext=text='Liberation Labs':fontcolor=0x546e7a:fontsize=20:x=(w-text_w)/2:y=(h-text_h)/2+80:font=monospace" \
    -c:v libx264 -preset fast -crf 20 \
    "$TMPDIR/opening.mp4" 2>/dev/null
echo "  Opening card"

# Closing card
ffmpeg -y -f lavfi -i "color=c=0x0a0e17:s=1280x720:d=5:r=24" \
    -vf "drawtext=text='SOVEREIGN MARKET INTELLIGENCE AGENT':fontcolor=0x4fc3f7:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2-30:font=monospace,drawtext=text='Let the agent cook.':fontcolor=0xfbbf24:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2+30:font=monospace" \
    -c:v libx264 -preset fast -crf 20 \
    "$TMPDIR/closing.mp4" 2>/dev/null
echo "  Closing card"

echo ""
echo "Step 3: Building timeline..."

FINAL="$TMPDIR/final_concat.txt"
> "$FINAL"

# Opening
echo "file '$TMPDIR/opening.mp4'" >> "$FINAL"

# Sections with titles + clips
for i in "${!SECTIONS_ORDER[@]}"; do
    section="${SECTIONS_ORDER[$i]}"
    clips="${SECTION_CLIPS[$section]}"

    if [ -n "$clips" ]; then
        echo "file '$TMPDIR/title_$i.mp4'" >> "$FINAL"
        for clip in $clips; do
            echo "file '$clip'" >> "$FINAL"
        done
    fi
done

# Closing
echo "file '$TMPDIR/closing.mp4'" >> "$FINAL"

TOTAL_ENTRIES=$(wc -l < "$FINAL")
echo "  $TOTAL_ENTRIES segments in timeline"

echo ""
echo "Step 4: Final render..."

ffmpeg -y -f concat -safe 0 -i "$FINAL" \
    -c:v libx264 -preset medium -crf 18 \
    -movflags +faststart \
    "$OUTPUT" 2>/dev/null

if [ -f "$OUTPUT" ]; then
    SIZE=$(du -h "$OUTPUT" | cut -f1)
    DURATION=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$OUTPUT" | cut -d. -f1)
    echo ""
    echo "╔══════════════════════════════════════╗"
    echo "║          VIDEO COMPLETE              ║"
    echo "╠══════════════════════════════════════╣"
    echo "║  Output: $OUTPUT"
    echo "║  Size:   $SIZE"
    echo "║  Length: ${DURATION}s"
    echo "║  Clips:  $COUNT AI-generated scenes"
    echo "╚══════════════════════════════════════╝"
else
    echo "ERROR: Output file not created"
    exit 1
fi
