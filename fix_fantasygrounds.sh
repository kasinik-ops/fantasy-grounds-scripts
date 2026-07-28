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
# 1. Offers simple reboot or permission-fix steps first.
# 2. Backs up your Fantasy Grounds data (campaigns, modules, etc.) to $HOME/FGBACKUP.
# 3. WILL PERMANENTLY DELETE your Fantasy Grounds preferences (plist and conf files).
# 4. WILL DELETE the application and data directories to perform a clean wipe.
################################################################################

# Function to display status messages
function status_message() {
    echo -e "\033[1;34m==> $1\033[0m"
}

function error_exit() {
    echo -e "\033[1;31mERROR: $1\033[0m" >&2
    exit 1
}

# Function to safely handle user-initiated reboot
function prompt_reboot() {
    read -p "Would you like to reboot your Mac now? (y/N): " reboot_choice
    case "$reboot_choice" in
        [Yy]* )
            echo "Rebooting system..."
            sudo shutdown -r now
            ;;
        * )
            echo "Skipping reboot. Please restart manually when ready."
            ;;
    esac
}

# Step 0: Non-Destructive Troubleshooting First
status_message "Initial Assessment: Quick Fix Recommendations"
echo "Before wiping your installation, it is recommended to try these simpler fixes:"
echo "1) Simply reboot your Mac."
echo "2) Repair Fantasy Grounds file permissions and reboot."
echo "3) Full clean reset:"
echo "   - Backs up your data (campaigns, modules, tokens) to $HOME/FGBACKUP"
echo "   - Purges RAM-cached preferences (cfprefsd) using 'defaults delete'"
echo "   - Deletes .plist & .conf files to reset resolution, UI, and login states"
echo "   - Completely removes app folders and reinstalls Fantasy Grounds"
echo ""
read -p "Choose an option (1, 2, or 3): " initial_option

case "$initial_option" in
    1)
        echo "A simple reboot often resolves memory-caching and process-lock issues."
        prompt_reboot
        exit 0
        ;;
    2)
        status_message "Attempting to fix executable permissions..."
        FG_APP="/Applications/SmiteWorks/Fantasy Grounds/FantasyGrounds.app"
        FG_UPDATER="/Applications/SmiteWorks/Fantasy Grounds/FantasyGroundsUpdater.app"

        if [ -d "$FG_APP" ] || [ -d "$FG_UPDATER" ]; then
            sudo chmod -R +x "$FG_APP" 2>/dev/null || true
            sudo chmod -R +x "$FG_UPDATER" 2>/dev/null || true
            echo "Permissions updated successfully."
            echo "It is recommended to reboot now to test if this resolves your issue."
            prompt_reboot
            exit 0
        else
            echo "Fantasy Grounds application folders not found at default paths."
            read -p "Press Enter to proceed to the full reset..." unused_var
        fi
        ;;
    3)
        echo "Proceeding to full wipe and reinstall..."
        ;;
    *)
        echo "Invalid option. Aborting."
        exit 1
        ;;
esac

# Initial Warning for Full Reset
echo ""
echo -e "\033[1;31m========================================================================\033[0m"
echo -e "\033[1;31mWARNING: THIS PROCESS WILL DELETE FANTASY GROUNDS PREFERENCES & DATA!\033[0m"
echo -e "\033[1;31m========================================================================\033[0m"
echo "While this script attempts an automated backup, IT IS SOLELY YOUR RESPONSIBILITY"
echo "to ensure that your data—ESPECIALLY YOUR FANTASY GROUNDS CAMPAIGN FOLDER—is safely"
echo "backed up to an external location before proceeding."
echo ""
read -p "Have you verified your backups and wish to proceed with a full wipe? (Type 'YES' to continue): " confirmation

if [ "$confirmation" != "YES" ]; then
    echo "Aborting script."
    exit 0
fi

# Variables
SRC_DIR="$HOME/SmiteWorks/Fantasy Grounds"
BACKUP_ROOT="$HOME/FGBACKUP"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="$BACKUP_ROOT/backup_$TIMESTAMP"

# Step 1: Dependency Check
status_message "Step 1: Checking system dependencies..."
for cmd in curl installer mktemp defaults; do
    command -v $cmd >/dev/null 2>&1 || error_exit "$cmd is not installed. Please install it and try again."
done

# Step 2: Backup
if [ -d "$SRC_DIR" ]; then
    status_message "Step 2: Creating a backup of your Fantasy Grounds data..."
    
    # Check disk space (approximate)
    available_space=$(df "$HOME" | awk 'NR==2 {print $4}')
    if [ "$available_space" -lt 1000000 ]; then # Less than ~1GB
        echo "Warning: Disk space is low. Backup might fail."
    fi

    mkdir -p "$BACKUP_DIR" || error_exit "Failed to create backup directory $BACKUP_DIR. Aborting script to protect your data."

    SUBDIRS=("campaigns" "extensions" "modules" "portraits" "tokens")
    for subdir in "${SUBDIRS[@]}"; do
        if [ -d "$SRC_DIR/$subdir" ]; then
            echo "Copying $subdir..."
            cp -R "$SRC_DIR/$subdir" "$BACKUP_DIR/" || error_exit "Backup failed while copying '$subdir'. Aborting script to prevent potential data loss."
        else
            echo "$subdir not found, skipping."
        fi
    done

    status_message "Backup completed successfully at $BACKUP_DIR"
else
    status_message "SmiteWorks/Fantasy Grounds directory not found. Nothing to backup."
fi

# Step 3: Preference Cleanup (plist)
status_message "Step 3: Removing plist files (memory and disk)..."
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

# Step 4: Config File Cleanup
status_message "Step 4: Removing configuration files..."
CONFIG_FILES=(
    "/Library/Preferences/SmiteWorks/fantasygrounds.conf"
    "$HOME/Library/Preferences/SmiteWorks/fantasygrounds.conf"
    "$HOME/Library/Preferences/SmiteWorks/fglauncher.conf"
    "$HOME/Library/Preferences/SmiteWorks/fguser.conf"
)

for config in "${CONFIG_FILES[@]}"; do
    if [ -f "$config" ]; then
        if [[ "$config" == "$HOME/Library/Preferences/SmiteWorks/"* ]]; then
            rm -f "$config" && echo "Deleted $config"
        else
            # Only use sudo if path is system-level
            sudo rm -f "$config" && echo "Deleted system config $config"
        fi
    fi
done

# Step 5: Directory Wipe
status_message "Step 5: Removing application directories..."
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

# Step 6: Install and Finish
status_message "Step 6: Downloading and Reinstalling..."
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

prompt_reboot
