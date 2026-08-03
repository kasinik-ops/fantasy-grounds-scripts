Overview
This repo is for scripts for Fantasy grounds. It is intended for my personal use but you are welcome to use them as you see fit. They have NO WARRANTY.

Claude (sonnet 5 and opus 4.8 extra) and Gemini (3.1 pro, 3.5 flash, 3.6 flash) were used to develop the scripts. They were lightly code reviewed by a human.


**fgu_maptest.py**

This script checks map files for issues which may cause performance issues and gives recommendations. It is read-only. Example output is in the image in this repo.

**fgu_maptest.py**

This script checks maps for issues which may cause performance issues and gives recommendations on problematic maps. It is read-only. Example output is in the image in this repo.


**fix_fantasygrounds.sh**

This script is a maintenance tool designed to resolve persistent crashing, update, and preference issues for Fantasy Grounds Unity on MacOS X. It performs a "Deep Clean" by purging memory-cached preferences and re-installing the application.

Data Safety Disclaimer: THIS DELETES DATA. The script attempts to create a campaign backup in `~/FGBACKUP`. However, always maintain a secondary manual backup of your `campaigns` folder. Backing up your campaigns is YOUR responsibility. 

Key Issues Addressed
This script specifically targets the following known macOS-specific problems:

1. The "Ghost Preference" Bug

The Issue: macOS uses a background service (`cfprefsd`) that caches `.plist` files in RAM. Simply deleting the files in Finder often fails because the OS restores the corrupted settings from memory. The Fix: This script uses the `defaults delete` command to force the OS to purge the cache.

2. Resolution & Window Crash (Retina Displays)

The Issue: On M1/M2/M3 Macs, the Unity engine can occasionally save an invalid window resolution (e.g., thousands of pixels wide), causing an immediate crash on launch. The Fix: Resetting `com.SmiteWorks.FantasyGrounds.plist` restores the default window size.

3. Stuck Updates & Login Failures

The Issue: Corrupted `.conf` files in the Library folder can prevent the updater from connecting or saving your license key. The Fix: Deleting `fglauncher.conf` and `fguser.conf` forces a fresh authentication.

4. Permission Errors (Broken Executable)

The Issue: Security updates or interrupted downloads can strip the "executable" bit from the app, making it unlaunchable. The Fix: A scripted reinstall via the official `.pkg` ensures all file permissions are set correctly by the macOS installer.


**classify_maps.py**

This uses classifies RPG battlemaps into kinds to make grabbing the right map before a session easier. Windows support is planned but not implemented yet.

On Mac OS X `./run_classifier_map.command` from the terminal (or double-click on it in the Mac OS X Finder) for a guided, dialog-based version of everything below, or call classify_maps.py directly for full control over every flag. The input can either be files in a folder or photos in an Apple photos album.

Classifies each map (PNG, JPG/JPEG, WEBP, HEIC/HEIF.) into one of the categories below using zero-shot CLIP classification (no training/dataset needed):

- **indoors**: castle&fort&tower, cave, church, crypt, dungeon, inn&house, manor, modern, scifi, unclassified
- **outdoors**: camp, castle&fort&tower, overgrowth, planar, regional, ruin, scifi, ship, site of interest, trail&bridge&travel, urban&town&city, village&rural, water&coast, wilderness
- **other**: map scenes, unclassified (for matches below the confience threshold, by default this is 25%)

The maps are then MOVED into category-named subfolders.  Apple Photos images are exported as classified copies into that destination (originals are left alone in Photos) and also get the category written back as a Photos keyword on the original.

It only scans files sitting directly in the selected folder, not subdirectories so re-running it won't re-classify maps a previous run already sorted into category subfolders.

`classify_maps.py`:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--dir DIR` | | Local folder of map images |
| `--album ALBUM` | | Apple Photos album name |
| `--unclassified-threshold` | `25.0` | Minimum CLIP confidence (%) before falling back to `unclassified` |
| `--move-to DIR` | prompt | Destination for sorted/exported files |
| `--no-move` | off | Classify only; don't move or export files |
| `-y`, `--yes` | off | Skip the move/export confirmation prompt |
| `--check-update` | off | Check Hugging Face for a model update now, bypassing the 24h throttle (still prompts) |
| `--force-update` | off | Check now and download an update without prompting, if one is found |
| `--no-update-check` | off | Skip the model-update check entirely; use whatever's cached (for limited/no internet) |


**detect_grid.py**

Detects if a grid exists (square grids only supported) on each image, and if it exists, creates a fantasy grounds compatible xml file with the grid pattern.

Unlike `classify_maps.py`, this scans recursively (including category subfolders), so it can reach maps already sorted into them by `classify_maps.py`. 

`detect_grid.py`:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--dir DIR` | | Local folder of map images |
| `--album ALBUM` | | Apple Photos album name |
| `-f`, `--force` | off | Overwrite an existing sidecar XML instead of skipping it |
| `--grid-min-px` | `20` | Smallest grid square (px) to consider |
| `--grid-max-fraction` | `0.25` | Largest grid square, as a fraction of the shorter image side |
| `--grid-min-confidence` | `0.15` | Minimum autocorrelation confidence (0-1) before reporting "no grid" |
| `--grid-min-line-coverage` | `0.5` | Minimum fraction (0-1) of the image a candidate grid line must actually span to count as real |

**Getting a map into Fantasy Grounds**

Neither `classify_map.py or` or `detect_grid.py` puts anything into Fantasy Grounds by itself as library can run into the thousands of files. To move the maps over:

1. Copy the map image and if it exists also the matching `.xml` (same base filename, e.g. `map.png` + `map.xml`) into your campaign's `images/` folder (flat, not in a subfolder).
2. In Fantasy Grounds: sidebar -> Library -> Assets -> Images, then click **Refresh Folder Assets** (bottom right) so it notices the new files. That same panel has a link (bottom right) that opens the images folder in Finder, handy for copying files over.
3. Click the image, then click **Create Image Record**.
4. In the opened map, click **Grid**, then click the eye icon to make the grid visible.



*Support scripts*

These scripts are for supporting purposes. You probably don't need to run these.

**fgu_telemetry.py**

This script captures Mac OS X system telemetry to help analysing map performance. It was used to generate the heuristics on map performance used in fgu_maptest.py

**fgu_telemetry_analyze.py**

This analyses the telemetry captured by fgu_telemetry.py

**test_safety.py**

Safety tests for `classify_maps.py` and `detect_grid.py`: checks that neither script does anything destructive beyond what's explicitly allowed (moving maps into category subfolders, creating/overwriting a map's `.xml` sidecar), and that neither ever overwrites an existing file without that being explicitly opted into (`-y` for moving, `-f`/`--force` for sidecar overwrite). Every write-capable call either script uses is intercepted for the duration of each run, and every path touched is checked against the exact directories that run was told to use Each script runs in-process so that interception can see every call; nothing about the scripts' own logic is mocked. A second, independent check snapshots the whole repo before and after and fails if anything in it changed at all. Only `--dir` mode is covered; `--album` mode's Photos-keyword write and osxphotos' own export aren't (no safe way to test against a real Photos library), and every `classify_maps.py` invocation passes `--no-update-check` so its model-update check is never exercised either. Run from inside the project `venv`:

```
source venv/bin/activate
python3 test_safety.py
```

The very first run will be slow while `classify_maps.py` downloads the CLIP model; it's cached after that, and every test here passes `--no-update-check` so none of them re-trigger a freshness check against huggingface.co.



