#!/bin/bash

# Exit immediately if a command exits with a non-zero status
# Treat unset variables as an error
set -euo pipefail

################################################################################
# MIT License
# 
# Copyright (c) 2026 [YOUR NAME]
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# THE SOFTWARE IS PROVIDED "AS-IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
################################################################################
# 
# SUMMARY OF ACTION:
# 1. Backs up your Fantasy Grounds data (campaigns, modules, etc.) to $HOME/FGBACKUP.
# 2. WILL PERMANENTLY DELETE your Fantasy Grounds preferences (plist and conf files).
# 3. WILL DELETE the application and data directories to perform a clean wipe.
################################################################################

# Function to display status messages
function status_message() {
    echo -e "\033[1;34m==> $1\033[0m"
}

function error_exit() {
    echo -e "\033[1;31mERROR: $1\033[0m" >&2
    exit 1
}

# Initial Warning
echo -e "\033[1;31mWARNING: This script will wipe Fantasy Grounds preferences and directories.\033[0m"
echo "It will attempt to backup your campaigns and assets first."
read -p "Are you sure you want to proceed? (Type 'YES' to continue): " confirmation

if [ "$confirmation" != "YES" ]; then
    echo "Aborting script."
    exit 0
fi

# Variables
SRC_DIR="$HOME/SmiteWorks/Fantasy Grounds"
BACKUP_ROOT="$HOME/FGBACKUP"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="$BACKUP_ROOT/backup_$TIMESTAMP"

# Step 0: Dependency Check
status_message "Step 0: Checking system dependencies..."
for cmd in curl installer mktemp defaults; do
    command -v $cmd >/dev/null 2>&1 || error_exit "$cmd is not installed. Please install it and try again."
done

# Step 1: Secure Backup
if [ -d "$SRC_DIR" ]; then
    status_message "Step 1: Creating a secure backup of your Fantasy Grounds data..."
    
    # Check disk space (approximate)
    available_space=$(df "$HOME" | awk 'NR==2 {print $4}')
    if [ "$available_space" -lt 1000000 ]; then # Less than ~1GB
        echo "Warning: Disk space is low. Backup might fail."
    fi

    mkdir -p "$BACKUP_DIR"

    SUBDIRS=("campaigns" "extensions" "modules" "portraits" "tokens")
    for subdir in "${SUBDIRS[@]}"; do
        if [ -d "$SRC_DIR/$subdir" ]; then
            echo "Copying $subdir..."
            cp -R "$SRC_DIR/$subdir" "$BACKUP_DIR/" || error_exit "Failed to copy $subdir. Aborting to prevent data loss."
        else
            echo "$subdir not found, skipping."
        fi
    done

    status_message "Backup completed successfully at $BACKUP_DIR"
else
    status_message "SmiteWorks/Fantasy Grounds directory not found. Nothing to backup."
fi

# Step 2: Preference Cleanup (plist)
status_message "Step 2: Removing plist files (memory and disk)..."
PLIST_FILES=(
    "unity.SmiteWorks.Fantasy Grounds"
    "unity.SmiteWorks.Fantasy Grounds Updater"
    "com.SmiteWorks.FGUpdaterEngine"
    "com.SmiteWorks.FantasyGrounds"
)

for plist in "${PLIST_FILES[@]}"; do
    defaults delete "$plist" 2>/dev/null || true
    
    plist_path="$HOME/Library/Preferences/$plist.plist"
    if [ -f "$plist_path" ]; then
        rm -f "$plist_path" && echo "Deleted $plist_path"
    fi
done

# Step 3: Config File Cleanup
status_message "Step 3: Removing configuration files..."
CONFIG_FILES=(
    "/Library/Preferences/SmiteWorks/fantasygrounds.conf"
    "$HOME/Library/Preferences/SmiteWorks/fantasygrounds.conf"
    "$HOME/Library/Preferences/SmiteWorks/fglauncher.conf"
    "$HOME/Library/Preferences/SmiteWorks/fguser.conf"
)

for config in "${CONFIG_FILES[@]}"; do
    if [ -f "$config" ]; then
        if [[ "$config" == "/Library/Preferences/SmiteWorks/"* ]]; then
            rm -f "$config" && echo "Deleted $config"
        else
            # Only use sudo if path is system-level
            sudo rm -f "$config" && echo "Deleted system config $config"
        fi
    fi
done

# Step 4: Directory Wipe
status_message "Step 4: Removing application directories..."
DIRS_TO_REMOVE=(
    "$HOME/SmiteWorks"
    "/Applications/SmiteWorks"
    "$HOME/Library/Preferences/SmiteWorks"
)

for dir in "${DIRS_TO_REMOVE[@]}"; do
    if [ -d "$dir" ]; then
        if [[ "$dir" == "/Applications/"* ]]; then
            sudo rm -rf "$dir" && echo "Removed $dir"
        else
            rm -rf "$dir" && echo "Removed $dir"
        fi
    fi
done

# Step 5: Install and Finish
status_message "Step 5: Downloading and Reinstalling..."
INSTALLER_URL="https://www.fantasygrounds.com/filelibrary/FGUWebInstall.pkg"
INSTALLER_PATH=$(mktemp /tmp/FGUInstall.XXXXXX.pkg)

if curl -o "$INSTALLER_PATH" -L "$INSTALLER_URL"; then
    sudo installer -pkg "$INSTALLER_PATH" -target /
    status_message "Fantasy Grounds reinstalled."
    rm -f "$INSTALLER_PATH"
else
    error_exit "Failed to download the installer."
fi

echo "-----------------------------------------------------------"
echo -e "\033[1;32mPROCESS COMPLETE.\033[0m"
echo -e "\033[1;33mCRITICAL: You MUST reboot your Mac now before opening Fantasy Grounds.\033[0m"
echo "-----------------------------------------------------------"
