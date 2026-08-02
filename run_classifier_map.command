#!/bin/bash
cd "$(dirname "$0")"

echo "=========================================="
echo " 🏰 RPG Map Pipeline Launcher"
echo "=========================================="

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

# 3. Choose execution pass
RUN_PASS=$(osascript -e 'button returned of (display dialog "Select Pipeline Action:" buttons {"Pass 1: Categorize", "Pass 2: FGU Grid", "Both Passes"} default button "Pass 1: Categorize")')

if [ "$RUN_PASS" == "Pass 1: Categorize" ]; then
    PASS_FLAG="--pass 1"
elif [ "$RUN_PASS" == "Pass 2: FGU Grid" ]; then
    PASS_FLAG="--pass 2"
else
    PASS_FLAG="--pass both"
fi

# 4. Environment Setup
if [ ! -d "venv" ]; then
    echo "📦 Creating first-time virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate
echo "🔄 Verifying dependencies..."
pip install --quiet --upgrade pip
pip install --quiet torch transformers pillow osxphotos opencv-python

# 5. Run Python script with GUI inputs passed as flags
echo "🚀 Starting processing pipeline..."
python classify_maps.py $MODE_FLAG "$LOCATION" $PASS_FLAG

echo ""
echo "✅ Pipeline complete! Press any key to close."
read -n 1
