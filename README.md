Overview
This repo is for scripts for Fantasy grounds. it is intended for my personal use but you are welcome to use them as you see fit.

It currently has two scripts.

fgu_maptest.py 
This script checks map files for issues which may cause performance issues and gives recommendations. It is read-only. Example outout:

./fgu_maptest.py --campaign ~/SmiteWorks/Fantasy\ Grounds/campaigns/Norse\ crystalpunk/
========================================================================================================
FGU MAP LOAD REPORT — each map is scored by its CUMULATIVE STATS
campaigns scanned: Norse crystalpunk
maps with LoS/lighting data: 18
bands (fine -> worst): GREEN  YELLOW  ORANGE  RED
score thresholds: YELLOW 31 | ORANGE 66 | RED 111
========================================================================================================
SEVERITY MAP                                DIMS     MP    SIZE   SEGS  LGT  SCORE (px lgt seg byt)
--------------------------------------------------------------------------------------------------------
RED      Jungle Ruin City            11700x11700  136.9   23.1M   3445    0    301 (137   0 118  46)
RED      Mountain base                 5728x9000   51.6    6.3M    689   62    271 ( 52 186  21  13)
RED      Soulspace - Rez               2520x2520    6.4    1.1M    121   82    258 (  6 246   4   2)
RED      Under forge kobold caverns    4942x3826   18.9    4.3M    339   52    194 ( 19 156  10   9)
RED      cave system                   9600x9600   92.2    7.6M    797    5    146 ( 92  15  24  15)
ORANGE   Crystalpunk office map        4096x4096   16.8   21.5M    538    4     88 ( 17  12  16  43)
ORANGE   Icy Realm                     7350x7350   54.0    6.5M      0    3     76 ( 54   9   0  13)
YELLOW   jungle lab                    4096x3072   12.6    7.5M     53    9     56 ( 13  27   2  15)
YELLOW   Escape from the office                ?      ?       ?    322   13     49 (  0  39  10   0)
YELLOW   Revelers street               2592x6192   16.0    1.5M    625    0     38 ( 16   0  19   3)
YELLOW   Shadow of Oblivion undergr    1792x1024    1.8  636.8K      0   10     33 (  2  30   0   1)
GREEN    City Street-Nightime with             ?      ?       ?      0    3      9 (  0   9   0   0)
GREEN    multi-story_carpark            883x2048    1.8  362.2K    211    0      9 (  2   0   6   1)
GREEN    office level 2                  555x741    0.4   72.5K    147    0      5 (  0   0   4   0)
GREEN    Office building ground          560x770    0.4   63.6K    122    0      4 (  0   0   4   0)
GREEN    Crystalpunk Industrial map     564x1039    0.6  110.6K     89    0      3 (  1   0   3   0)
GREEN    forest maze                     736x736    0.5  132.4K     78    0      3 (  1   0   2   0)
GREEN    Office level 1                  557x742    0.4  106.5K     75    0      3 (  0   0   2   0)
--------------------------------------------------------------------------------------------------------
band tally   RED: 5   ORANGE: 2   YELLOW: 4   GREEN: 7

WORST OFFENDERS (start here):
  • Jungle Ruin City: 137 pts from Resolution (11700x11700), 118 pts from LoS (3445 segments), 46 pts from Size ( 23.1M)
      -> Action: Downscale image to <= 4000x4000 pixels in an external editor before importing.
      -> Action: Compress image or convert to WEBP format to reduce file size below 10MB.
      -> Action: Use FG's 'Simplify' tool on LoS lines (Unlock Map > Line of Sight > Select lines > Simplify).
  • Mountain base: 52 pts from Resolution (5728x9000), 186 pts from Lights (62 lights), 21 pts from LoS (689 segments)
      -> Action: Downscale image to <= 4000x4000 pixels in an external editor before importing.
      -> Action: Use FG's 'Simplify' tool on LoS lines (Unlock Map > Line of Sight > Select lines > Simplify).
      -> Action: Reduce light count. Remove overlapping/redundant lights or switch to Global Ambient Lighting.
  • Soulspace - Rez: 246 pts from Lights (82 lights)
      -> Action: Reduce light count. Remove overlapping/redundant lights or switch to Global Ambient Lighting.
  • Under forge kobold caverns: 156 pts from Lights (52 lights)
      -> Action: Reduce light count. Remove overlapping/redundant lights or switch to Global Ambient Lighting.
  • cave system: 92 pts from Resolution (9600x9600), 24 pts from LoS (797 segments)
      -> Action: Downscale image to <= 4000x4000 pixels in an external editor before importing.
      -> Action: Use FG's 'Simplify' tool on LoS lines (Unlock Map > Line of Sight > Select lines > Simplify).

CAMPAIGN-LEVEL SIGNALS (hit every client, not just one map):
  [Norse crystalpunk] db.xml 4.3 MB
    combat tracker: 8 combatants, 56 active effects, 2 light-emitting <- token-movement freeze trigger

fix_fantasygrounds.sh
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

