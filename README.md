Overview
This script is a maintenance tool designed to resolve persistent crashing, update, and preference issues for Fantasy Grounds Unity on macOS. It performs a "Deep Clean" by purging memory-cached preferences and re-installing the application.

Data Safety: The script creates a backup in `~/FGBACKUP`. However, always maintain a secondary manual backup of your `campaigns` folder.

No warranty is implied for this script. Backing up your campaigns is YOUR responsibility.

Key Issues Addressed
This script specifically targets the following known macOS-specific problems:

1. The "Ghost Preference" Bug

The Issue: macOS uses a background service (`cfprefsd`) that caches `.plist` files in RAM. Simply deleting the files in Finder often fails because the OS restores the corrupted settings from memory. The Fix: This script uses the `defaults delete` command to force the OS to purge the cache.

2. Resolution & Window Crash (Retina Displays)

The Issue: On M1/M2/M3 Macs, the Unity engine can occasionally save an invalid window resolution (e.g., thousands of pixels wide), causing an immediate crash on launch. The Fix: Resetting `com.SmiteWorks.FantasyGrounds.plist` restores the default window size. Reference: [Fantasy Grounds Forums - Mac Crash Discussion]([suspicious link removed])

3. Stuck Updates & Login Failures

The Issue: Corrupted `.conf` files in the Library folder can prevent the updater from connecting or saving your license key. The Fix: Deleting `fglauncher.conf` and `fguser.conf` forces a fresh authentication. Reference: [Fantasy Grounds Wiki - Troubleshooting]([suspicious link removed])

4. Permission Errors (Broken Executable)

The Issue: Security updates or interrupted downloads can strip the "executable" bit from the app, making it unlaunchable. The Fix: A scripted reinstall via the official `.pkg` ensures all file permissions are set correctly by the macOS installer.

Disclaimer & Safety
• Warranty: Provided "as-is" without warranty.

• Data Safety: The script creates a backup in `~/FGBACKUP`. However, always maintain a secondary manual backup of your `campaigns` folder.

• Reboot Required: You must reboot after the script finishes to ensure the system cache is fully cleared.

