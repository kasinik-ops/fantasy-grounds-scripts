#!/bin/bash
cd "$(dirname "$0")"

echo "=========================================="
echo " 🗺️  RPG Map Tools"
echo "=========================================="

# 1. Environment Setup (everything the classifier, the grid detector and the finder need;
#    osxphotos is only for the command-line --album option, so it isn't installed here)
if [ ! -d "venv" ]; then
    echo "📦 Creating first-time virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate
echo "🔄 Verifying dependencies..."
# No --upgrade, so this makes no network request when nothing is missing. The package
# ranges live in the requirements files (shared with the other launchers and CI).
# --default-timeout keeps this from hanging if a package genuinely does need to be
# fetched with no connection.
pip install --quiet --default-timeout=10 -r requirements-classify.txt -r requirements-grid.txt

# 2. Start the window. It opens in your web browser; closing this Terminal window
#    (or pressing Ctrl+C) stops it.
echo "🚀 Starting the map tools..."
python fgu_ui.py

echo ""
echo "✅ Stopped. Press any key to close."
read -n 1
