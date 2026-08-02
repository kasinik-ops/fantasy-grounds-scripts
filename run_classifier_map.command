#!/bin/bash
cd "$(dirname "$0")"

echo "=========================================="
echo " 🏰 RPG Map Pipeline Launcher"
echo "=========================================="

# Escapes a string for safe embedding inside a double-quoted AppleScript string literal.
osa_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# 1. Ask user for source type using macOS dialog popup
SOURCE_TYPE=$(osascript -e 'button returned of (display dialog "Where are your battle maps stored?" buttons {"Local Folder", "Apple Photos Album", "Cancel"} default button "Local Folder")')

if [ "$SOURCE_TYPE" == "Cancel" ]; then
    echo "Canceled."
    exit 0
fi

# 2. Get location path or album name
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

# 3. Choose execution pass
RUN_PASS=$(osascript -e 'button returned of (display dialog "Select Pipeline Action:" buttons {"Pass 1: Categorize", "Pass 2: FGU Grid", "Both Passes"} default button "Pass 1: Categorize")')

if [ "$RUN_PASS" == "Pass 1: Categorize" ]; then
    PY_ARGS+=(--pass 1)
elif [ "$RUN_PASS" == "Pass 2: FGU Grid" ]; then
    PY_ARGS+=(--pass 2)
else
    PY_ARGS+=(--pass both)
fi

RUNS_PASS_1=false
RUNS_PASS_2=false
if [ "$RUN_PASS" != "Pass 2: FGU Grid" ]; then
    RUNS_PASS_1=true
fi
if [ "$RUN_PASS" != "Pass 1: Categorize" ]; then
    RUNS_PASS_2=true
fi

# 4. Pass 2: overwrite-existing-sidecar toggle
if [ "$RUNS_PASS_2" == true ]; then
    OVERWRITE_CHOICE=$(osascript -e 'button returned of (display dialog "Pass 2: some maps may already have an FGU grid sidecar (.xml) file. Overwrite existing sidecars, or skip them?" buttons {"Skip Existing", "Overwrite"} default button "Skip Existing")')
    if [ "$OVERWRITE_CHOICE" == "Overwrite" ]; then
        PY_ARGS+=(--force)
    fi
fi

# 5. Pass 1: destination directory to sort classified files into by category.
# The launcher only asks WHERE; classify_maps.py itself warns how many files are
# affected and asks for confirmation before moving/exporting anything.
if [ "$RUNS_PASS_1" == true ]; then
    if [ "$SOURCE_TYPE" == "Local Folder" ]; then
        MOVE_DEST=$(osascript -e "text returned of (display dialog \"Pass 1: destination directory to sort classified files into (a subfolder per category will be created inside it):\" default answer \"$(osa_escape "$LOCATION")\")")
    else
        MOVE_DEST=$(osascript -e 'text returned of (display dialog "Pass 1: destination directory to sort classified files into (a subfolder per category will be created inside it). Originals stay in Photos -- classified COPIES are written here:" default answer "")')
    fi
    if [ -n "$MOVE_DEST" ]; then
        PY_ARGS+=(--move-to "$MOVE_DEST")
    else
        PY_ARGS+=(--no-move)
    fi
fi

# 6. Advanced detection parameters (optional)
CUSTOMIZE=$(osascript -e 'button returned of (display dialog "Use default detection settings, or customize advanced parameters?" buttons {"Use Defaults", "Customize"} default button "Use Defaults")')

if [ "$CUSTOMIZE" == "Customize" ]; then
    if [ "$RUNS_PASS_1" == true ]; then
        UNCLASSIFIED_THRESHOLD=$(osascript -e 'text returned of (display dialog "Pass 1: minimum CLIP confidence (%) before tagging an image \"unclassified\" instead of forcing the closest category:" default answer "25")')
        if [ -n "$UNCLASSIFIED_THRESHOLD" ]; then
            PY_ARGS+=(--unclassified-threshold "$UNCLASSIFIED_THRESHOLD")
        fi
    fi
    if [ "$RUNS_PASS_2" == true ]; then
        GRID_MIN_PX=$(osascript -e 'text returned of (display dialog "Pass 2: minimum grid square size to consider, in pixels:" default answer "20")')
        if [ -n "$GRID_MIN_PX" ]; then
            PY_ARGS+=(--grid-min-px "$GRID_MIN_PX")
        fi

        GRID_MAX_FRACTION=$(osascript -e 'text returned of (display dialog "Pass 2: maximum grid square size, as a fraction of the shorter image side:" default answer "0.25")')
        if [ -n "$GRID_MAX_FRACTION" ]; then
            PY_ARGS+=(--grid-max-fraction "$GRID_MAX_FRACTION")
        fi

        GRID_MIN_CONFIDENCE=$(osascript -e 'text returned of (display dialog "Pass 2: minimum grid-detection confidence (0-1) before reporting \"no grid detected\":" default answer "0.15")')
        if [ -n "$GRID_MIN_CONFIDENCE" ]; then
            PY_ARGS+=(--grid-min-confidence "$GRID_MIN_CONFIDENCE")
        fi

        GRID_MIN_LINE_COVERAGE=$(osascript -e 'text returned of (display dialog "Pass 2: minimum fraction (0-1) of the image a candidate grid line must actually span to count as real (rejects photo textures like railings or decking):" default answer "0.5")')
        if [ -n "$GRID_MIN_LINE_COVERAGE" ]; then
            PY_ARGS+=(--grid-min-line-coverage "$GRID_MIN_LINE_COVERAGE")
        fi
    fi
fi

# 7. Environment Setup
if [ ! -d "venv" ]; then
    echo "📦 Creating first-time virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate
echo "🔄 Verifying dependencies..."
pip install --quiet --upgrade pip
# osxphotos>=0.67 requires Python 3.10+ (uses `X | None` type syntax) and fails to
# import at all on the 3.9 interpreter macOS ships by default -- pin below that.
pip install --quiet torch transformers pillow pillow-heif "osxphotos<0.67" opencv-python numpy

# 8. Run Python script with GUI inputs passed as flags
echo "🚀 Starting processing pipeline..."
python classify_maps.py "${PY_ARGS[@]}"

echo ""
echo "✅ Pipeline complete! Press any key to close."
read -n 1
