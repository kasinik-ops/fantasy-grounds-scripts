#!/bin/bash
cd "$(dirname "$0")"

echo "=========================================="
echo " 🏰 RPG Map Categorizer Launcher"
echo "=========================================="

# Escapes a string for safe embedding inside a double-quoted AppleScript string literal.
osa_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# 1. Explain what this does before touching anything, with a way to read more.
INTRO_MSG=$'This tool uses AI image classification to sort your battle maps into category folders (dungeon, ruin, tavern, and so on), so they are easier to browse and manage.\n\nThis does NOT put maps into Fantasy Grounds. Once you have picked out the maps you actually want to use, you still need to manually copy them (and their grid .xml file, if you have run the grid detector) into your Fantasy Grounds campaign images folder.\n\nSee the README for full details.'

while true; do
    INTRO_CHOICE=$(osascript -e "button returned of (display dialog \"$(osa_escape "$INTRO_MSG")\" buttons {\"Cancel\", \"Open README\", \"Continue\"} default button \"Continue\" with title \"RPG Map Categorizer\")")
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

# 4. Destination directory to sort classified files into by category.
# The launcher only asks WHERE; classify_maps.py itself warns how many files are
# affected and asks for confirmation before moving/exporting anything.
if [ "$SOURCE_TYPE" == "Local Folder" ]; then
    MOVE_DEST=$(osascript -e "text returned of (display dialog \"Destination directory to sort classified files into (a subfolder per category will be created inside it):\" default answer \"$(osa_escape "$LOCATION")\")")
else
    MOVE_DEST=$(osascript -e 'text returned of (display dialog "Destination directory to sort classified files into (a subfolder per category will be created inside it). Originals stay in Photos -- classified COPIES are written here:" default answer "")')
fi
if [ -n "$MOVE_DEST" ]; then
    PY_ARGS+=(--move-to "$MOVE_DEST")
else
    PY_ARGS+=(--no-move)
fi

# 5. Advanced detection parameters (optional)
CUSTOMIZE=$(osascript -e 'button returned of (display dialog "Use default detection settings, or customize advanced parameters?" buttons {"Use Defaults", "Customize"} default button "Use Defaults")')

if [ "$CUSTOMIZE" == "Customize" ]; then
    UNCLASSIFIED_THRESHOLD=$(osascript -e 'text returned of (display dialog "Minimum CLIP confidence (%) before tagging an image \"unclassified\" instead of forcing the closest category:" default answer "25")')
    if [ -n "$UNCLASSIFIED_THRESHOLD" ]; then
        PY_ARGS+=(--unclassified-threshold "$UNCLASSIFIED_THRESHOLD")
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
# check for a newer pip, which is the same kind of unnecessary always-on
# network dependency classify_maps.py's own model-update check now avoids.
# The venv's bundled pip is good enough. --default-timeout keeps this from
# hanging if a package genuinely does need to be fetched with no connection.
# osxphotos>=0.67 requires Python 3.10+ (uses `X | None` type syntax) and fails
# to import at all on the 3.9 interpreter macOS ships by default -- pin below.
pip install --quiet --default-timeout=10 torch transformers pillow pillow-heif "osxphotos<0.67"

# 7. Run Python script with GUI inputs passed as flags
echo "🚀 Starting classification..."
python classify_maps.py "${PY_ARGS[@]}"

echo ""
echo "✅ Done! Press any key to close."
read -n 1
