Overview
This repo is for scripts for Fantasy grounds. it is intended for my personal use but you are welcome to use them as you see fit. They have NO WARRANTY.


**fgu_maptest.py**

This script checks map files for issues which may cause performance issues and gives recommendations. It is read-only. Example outout is in the image in this repo.

**fgu_telemetry.py**

This script captures Mac OS X system telemetry to help analysing map performance. It was used to generate the heuristics on map performance used in fgu_maptest.py

**fgu_telemetry_analyze.py**

This analyses the telemetry captured by fgu_telemetry.py

**fix_fantasygrounds.sh**

This script is a maintenance tool designed to resolve persistent crashing, update, and preference issues for Fantasy Grounds Unity on macOS. It performs a "Deep Clean" by purging memory-cached preferences and re-installing the application.

Data Safety: The script creates a backup in `~/FGBACKUP`. However, always maintain a secondary manual backup of your `campaigns` folder. Backing up your campaigns is YOUR responsibility.

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

**classify_maps.py / run_classifier_map.command**

A two-pass pipeline for organizing and prepping TTRPG battle maps. Pass 1 uses CLIP (an AI image classifier) to sort maps into categories; Pass 2 detects each map's grid and writes a Fantasy Grounds Unity sidecar XML file for it. Works against either a local folder of images or an Apple Photos album.

Run `./run_classifier_map.command` (double-clickable in Finder) for a guided, dialog-based version of everything below, or call `classify_maps.py` directly for full control over every flag. The launcher sets up its own `venv` and installs dependencies (`torch`, `transformers`, `pillow`, `pillow-heif`, `osxphotos<0.67`, `opencv-python`, `numpy`) on first run. Supported image types: PNG, JPG/JPEG, WEBP, HEIC/HEIF.

*Pass 1: Categorize*

Classifies each map into one of the categories below using zero-shot CLIP classification (no training/dataset needed):

- **indoors**: castle&fort&tower, cave, church, crypt, dungeon, inn&house, manor, modern, scifi, unclassified
- **outdoors**: camp, castle&fort&tower, overgrowth, planar, regional, ruin, scifi, ship, site of interest, trail&bridge&travel, urban&town&city, village&rural, water&coast, wilderness
- **other**: map scenes, unclassified

Low-confidence matches are tagged `unclassified` instead of being forced into a poorly-fitting category (`--unclassified-threshold`, default 25%) -- CLIP always returns its best guess even when nothing really fits.

Classified files then get sorted: local-folder images are moved into category-named subfolders under a destination directory you're asked for; Apple Photos images are exported as classified copies into that destination (originals are left alone in Photos -- moving the real library file directly risks corrupting it) and also get the category written back as a Photos keyword on the original. Before moving/exporting anything, the pipeline always prints how many files are affected and where, and asks for confirmation (`-y`/`--yes` to skip the prompt, `--no-move` to disable moving entirely, `--move-to <dir>` to set the destination without being asked).

Pass 1 only scans files sitting directly in the selected folder, not subdirectories -- so re-running it won't re-classify maps a previous run already sorted into category subfolders.

*Pass 2: FGU Grid*

Detects each map's grid (square size + pixel offset) straight from the image -- no manual measuring. Writes `<mapfile>.xml` next to the image: `<gridsize>` is a verified Fantasy Grounds Unity sidecar field (cross-checked against the [Imagix/uvtt2fgu](https://github.com/Imagix/uvtt2fgu) reference tool); `<gridoffset>` is a best-effort guess that hasn't been confirmed as something FGU actually reads, so test-import one map and check the grid lines up before trusting it across a whole library.

Unlike Pass 1, this scans recursively (including category subfolders), so it can reach maps Pass 1 already sorted. It skips images that already have a sidecar XML by default, to avoid clobbering hand-tuned values (`-f`/`--force` to overwrite instead). Detected grids are also rejected unless the candidate lines actually span most of the image, not just look periodic in aggregate -- this stops photos with repeating texture (railings, decking, portholes) from being scored as if they had a real drawn battle-map grid.

*Getting a classified map into Fantasy Grounds*

Sidecar XML only takes effect once both the image and its `.xml` are copied into your campaign's `images/` folder (flat, not in a subfolder), keeping the same filename. This is a deliberate manual step, not automated by this pipeline, since a library can run into the thousands of files. Once copied, import the image into FGU as normal and check the grid.

*Command-line flags*

| Flag | Pass | Default | Purpose |
| --- | --- | --- | --- |
| `--dir DIR` | both | | Local folder of map images |
| `--album ALBUM` | both | | Apple Photos album name |
| `--pass {1,2,both}` | | `1` | Which pass(es) to run |
| `-f`, `--force` | 2 | off | Overwrite an existing sidecar XML instead of skipping it |
| `--unclassified-threshold` | 1 | `25.0` | Minimum CLIP confidence (%) before falling back to `unclassified` |
| `--move-to DIR` | 1 | prompt | Destination for sorted/exported files |
| `--no-move` | 1 | off | Classify only; don't move or export files |
| `-y`, `--yes` | 1 | off | Skip the move/export confirmation prompt |
| `--grid-min-px` | 2 | `20` | Smallest grid square (px) to consider |
| `--grid-max-fraction` | 2 | `0.25` | Largest grid square, as a fraction of the shorter image side |
| `--grid-min-confidence` | 2 | `0.15` | Minimum autocorrelation confidence (0-1) before reporting "no grid" |
| `--grid-min-line-coverage` | 2 | `0.5` | Minimum fraction (0-1) of the image a candidate grid line must actually span to count as real |

