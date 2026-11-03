#!/bin/bash
cd "$(dirname "$0")"

echo "=========================================="
echo " 🔎 RPG Map Finder Launcher"
echo "=========================================="

# Escapes a string for safe embedding inside a double-quoted AppleScript string literal.
osa_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# 1. Explain what this does before touching anything, with a way to read more.
INTRO_MSG=$'This tool finds battle maps by description (for example "tavern interior at night"), or shows your whole map library as a page of pictures you can click through. It looks at the pictures themselves, so it also finds maps hiding in folders with unhelpful names.\n\nIt only READS your maps. It keeps a small index and thumbnails in a hidden folder called .fgu_map_index inside your maps folder, which is safe to delete at any time. The first time, it reads every map once, which can take a while for a big library; after that only new maps are read.\n\nIt needs an AI model (about 600 MB). If it is not on this computer yet, it is downloaded once, before the first search, and that is the only time this tool goes online.\n\nThe results open as a page in your web browser. See the README for full details.'

while true; do
    INTRO_CHOICE=$(osascript -e "button returned of (display dialog \"$(osa_escape "$INTRO_MSG")\" buttons {\"Cancel\", \"Open README\", \"Continue\"} default button \"Continue\" with title \"RPG Map Finder\")")
    if [ "$INTRO_CHOICE" == "Open README" ]; then
        open -t "README.md"
    elif [ "$INTRO_CHOICE" == "Continue" ]; then
        break
    else
        echo "Canceled."
        exit 0
    fi
done

# 2. Which folder of maps.
LOCATION=$(osascript -e 'POSIX path of (choose folder with prompt "Select your Battle Maps folder:")')
if [ -z "$LOCATION" ]; then
    echo "Canceled."
    exit 0
fi

# 3. Search, or a picture page of everything. The command goes first, then --dir.
MODE=$(osascript -e 'button returned of (display dialog "Search for maps matching a description, or make a picture page of the whole library?" buttons {"Cancel", "Whole Library", "Search"} default button "Search" with title "RPG Map Finder")')

if [ "$MODE" == "Search" ]; then
    QUERY=$(osascript -e 'text returned of (display dialog "Describe the map you are looking for, for example: tavern interior at night" default answer "" with title "RPG Map Finder")')
    if [ -z "$QUERY" ]; then
        echo "Canceled."
        exit 0
    fi
    PY_ARGS=(search --dir "$LOCATION" "$QUERY" --open)
elif [ "$MODE" == "Whole Library" ]; then
    PY_ARGS=(sheet --dir "$LOCATION" --open)
else
    echo "Canceled."
    exit 0
fi

# 4. Environment Setup
if [ ! -d "venv" ]; then
    echo "📦 Creating first-time virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate
echo "🔄 Verifying dependencies..."
# No --upgrade, so this makes no network request when nothing is missing.
# The package ranges live in requirements-find.txt (shared with the other
# launchers and CI). --default-timeout keeps this from hanging if a package
# genuinely does need to be fetched with no connection.
pip install --quiet --default-timeout=10 -r requirements-find.txt

# 4b. The AI model. If it is not in the cache yet, fetch it now (the only time this tool
# goes online); otherwise this makes no network request.
if [ ! -d "${HF_HOME:-$HOME/.cache/huggingface}/hub/models--openai--clip-vit-base-patch32" ]; then
    echo "⬇️  The AI model is not on this computer yet. Downloading it now (about 600 MB, needs internet, once only)..."
    python fgu_find_maps.py download || { echo "❌ Downloading the AI model failed. Check the internet connection and try again."; exit 1; }
fi

# 5. Run Python script with GUI inputs passed as flags
echo "🚀 Starting map finder..."
python fgu_find_maps.py "${PY_ARGS[@]}"

echo ""
echo "✅ Done! Press any key to close."
read -n 1
