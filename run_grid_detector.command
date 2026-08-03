#!/bin/bash
cd "$(dirname "$0")"

echo "=========================================="
echo " 🏰 RPG Map Grid Detector Launcher"
echo "=========================================="

# Escapes a string for safe embedding inside a double-quoted AppleScript string literal.
osa_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# 1. Explain what this does before touching anything, with a way to read more.
INTRO_MSG=$'This tool detects each map battle-map grid and writes a Fantasy Grounds sidecar .xml file next to it, so the grid lines up automatically when imported.\n\nThis does NOT put maps into Fantasy Grounds. You still need to manually copy both the map image and its .xml file into your campaign images folder (same folder, not a subfolder).\n\nThen in Fantasy Grounds: in the sidebar go to Library, click Assets, click Images, then click "Refresh Folder Assets" (bottom right). Click the image, then click "Create Image Record". In the opened map, click Grid, then click the eye icon to make the grid visible.\n\nTip: the Images panel also has a link (bottom right) that opens the images folder in Finder, handy for copying files over.\n\nSee the README for full details.'

while true; do
    INTRO_CHOICE=$(osascript -e "button returned of (display dialog \"$(osa_escape "$INTRO_MSG")\" buttons {\"Cancel\", \"Open README\", \"Continue\"} default button \"Continue\" with title \"RPG Map Grid Detector\")")
    if [ "$INTRO_CHOICE" == "Cancel" ]; then
        echo "Canceled."
        exit 0
    elif [ "$INTRO_CHOICE" == "Open README" ]; then
        open -t "README.md"
    else
        break
    fi
done

# 2. Ask user for source type using macOS dialog popup
SOURCE_TYPE=$(osascript -e 'button returned of (display dialog "Where are your battle maps stored?" buttons {"Local Folder", "Apple Photos Album", "Cancel"} default button "Local Folder")')

if [ "$SOURCE_TYPE" == "Cancel" ]; then
    echo "Canceled."
    exit 0
fi

# 3. Get location path or album name
if [ "$SOURCE_TYPE" == "Local Folder" ]; then
    LOCATION=$(osascript -e 'POSIX path of (choose folder with prompt "Select your Battle Maps folder:")')
    MODE_FLAG="--dir"
else
    LOCATION=$(osascript -e 'text returned of (display dialog "Enter your Apple Photos Album Name:" default answer "RPG Battle Maps")')
    MODE_FLAG="--album"
fi

if [ -z "$LOCATION" ]; then
    echo "Canceled."
    exit 0
fi

PY_ARGS=("$MODE_FLAG" "$LOCATION")

# 4. Overwrite-existing-sidecar toggle
OVERWRITE_CHOICE=$(osascript -e 'button returned of (display dialog "Some maps may already have an FGU grid sidecar (.xml) file. Overwrite existing sidecars, or skip them?" buttons {"Skip Existing", "Overwrite"} default button "Skip Existing")')
if [ "$OVERWRITE_CHOICE" == "Overwrite" ]; then
    PY_ARGS+=(--force)
fi

# 5. Advanced detection parameters (optional)
CUSTOMIZE=$(osascript -e 'button returned of (display dialog "Use default detection settings, or customize advanced parameters?" buttons {"Use Defaults", "Customize"} default button "Use Defaults")')

if [ "$CUSTOMIZE" == "Customize" ]; then
    GRID_MIN_PX=$(osascript -e 'text returned of (display dialog "Minimum grid square size to consider, in pixels:" default answer "20")')
    if [ -n "$GRID_MIN_PX" ]; then
        PY_ARGS+=(--grid-min-px "$GRID_MIN_PX")
    fi

    GRID_MAX_FRACTION=$(osascript -e 'text returned of (display dialog "Maximum grid square size, as a fraction of the shorter image side:" default answer "0.25")')
    if [ -n "$GRID_MAX_FRACTION" ]; then
        PY_ARGS+=(--grid-max-fraction "$GRID_MAX_FRACTION")
    fi

    GRID_MIN_CONFIDENCE=$(osascript -e 'text returned of (display dialog "Minimum grid-detection confidence (0-1) before reporting \"no grid detected\":" default answer "0.15")')
    if [ -n "$GRID_MIN_CONFIDENCE" ]; then
        PY_ARGS+=(--grid-min-confidence "$GRID_MIN_CONFIDENCE")
    fi

    GRID_MIN_LINE_COVERAGE=$(osascript -e 'text returned of (display dialog "Minimum fraction (0-1) of the image a candidate grid line must actually span to count as real (rejects photo textures like railings or decking):" default answer "0.5")')
    if [ -n "$GRID_MIN_LINE_COVERAGE" ]; then
        PY_ARGS+=(--grid-min-line-coverage "$GRID_MIN_LINE_COVERAGE")
    fi
fi

# 6. Environment Setup
if [ ! -d "venv" ]; then
    echo "📦 Creating first-time virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate
echo "🔄 Verifying dependencies..."
# No --upgrade here: that would hit the network on every single launch just to
# check for a newer pip. The venv's bundled pip is good enough.
# --default-timeout keeps this from hanging if a package genuinely does need
# to be fetched with no connection.
pip install --quiet --default-timeout=10 pillow pillow-heif opencv-python numpy "osxphotos<0.67"

# 7. Run Python script with GUI inputs passed as flags
echo "🚀 Starting grid detection..."
python detect_grid.py "${PY_ARGS[@]}"

echo ""
echo "✅ Done! Press any key to close."
read -n 1
