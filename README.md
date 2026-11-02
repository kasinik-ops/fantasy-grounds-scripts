*Overview*
This repo is for scripts for Fantasy grounds. It is intended for my personal use but you are welcome to use them as you see fit. They have NO WARRANTY.

Claude (sonnet 5 and opus 4.8 extra) and Gemini (3.1 pro, 3.5 flash, 3.6 flash) were used to develop the scripts. They were lightly code reviewed by a human.


**fgu_support_maptest.py**

This script checks maps for issues which may cause performance issues and gives recommendations on problematic maps. It is read-only. Example output is in the image in this repo.


**fgu_fix_macos.sh**

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


**fgu_ui.py** (the map tools window)

A basic window for the map tools below, the same on Mac OS X and Windows. Double-click `run_ui.command` (Mac) or `run_ui.bat` (Windows). The first run sets up Python and the packages if they aren't there yet, then your web browser opens on a page served from your own computer. Closing the terminal window it started in stops it. (From the command line: `python fgu_ui.py`, optionally with `--library "/path/to/maps"` to start on a folder.)

Type the full path of your maps folder at the top (it remembers the last one) and press **Use this folder**. The line under it shows counts only: how many maps are indexed, whether the AI model is downloaded, and how many maps sit directly in that folder. Then:

- **Find**: describe a map and see the best matches as thumbnails. Choose how many (12, 24, 48 or 100). **Open map** opens the real file in your computer's normal viewer.
- **Browse**: pick a folder from the list (names and counts) and page through it, 48 maps at a time.
- **Sort**: the classifier. **Preview** moves nothing and writes a CSV report into the folder. **Sort the maps** moves them into category folders inside that folder, and only if you tick the confirmation box; it always saves an undo file. **Undo** puts a previous sort back, again only with the box ticked. Like the classifier itself, this looks only at maps directly inside the folder, not its sub-folders. It works on the folder in its own **Folder to sort** box, which defaults to the folder at the top.
- **Grids**: the grid detector, which looks at a folder and its sub-folders. The **Folder** box defaults to the folder at the top (the whole library); pick one of your library's folders from its drop-down, or type the path of any folder on the computer, to run it on just that one. Maps that already have an `.xml` are skipped unless you tick both boxes to replace them.
- **Check**: the map-load report from `fgu_support_maptest.py`. Point it at a Fantasy Grounds campaign folder (or leave it blank to look through the campaigns in Fantasy Grounds' own data folder) and it ranks the campaign's maps by how heavy they are to display, worst first, showing 10 to 100 of them. It only reads the campaign's files.
- **Setup**: **Update AI model cache** (marked "Requires internet access": it is the one button that goes online, and only when you press it; it downloads the model the first time and checks for newer files after that) and build or update the index.

Whatever you start runs the existing script with the same flags you would type, and its output appears live in the log at the bottom, with a Stop button. One thing runs at a time. Not included on purpose: `fgu_fix_macos.sh`, which deletes data and should stay a deliberate command-line step, and the telemetry scripts, which are developer tools. Because each button just runs the existing script, all of that script's safety rules apply unchanged (nothing is overwritten; moves are never silent). The window itself writes no files.

It deliberately never shows the whole library at once. Browsing is 48 maps per page, a search shows at most 100, the folder list is capped at 500 names (with a count of any more), the status line shows counts rather than lists, and the log keeps the last 3,000 lines. Only maps that are in the index can be browsed, found or opened, so the first thing to do with a new library is **Setup, then Build / update the index**. That first build reads every map once and can take a long time on a big library, but you can leave it running, and a re-run carries on where it stopped.

Who can use it: only your own computer. It listens on `127.0.0.1` and ignores requests addressed any other way, and it needs the random one-time address it prints (the browser keeps it in a cookie), so other web pages you have open can't drive it. There is no password because nothing is reachable from outside your machine.

Limits: you type or paste the folder path (there is no folder-picker button), the page needs a normal web browser, and it has not been tried on screens smaller than a laptop.

**fgu_classify_maps.py**

This uses classifies RPG battlemaps into kinds to make grabbing the right map before a session easier. It runs on Mac OS X and on Windows (see **Running on Windows** below).

On Mac OS X `./run_classifier_map.command` from the terminal (or double-click on it in the Mac OS X Finder) for a guided, dialog-based version of everything below, or call fgu_classify_maps.py directly for full control over every flag. The input can either be files in a folder or photos in an Apple photos album.

Classifies each map (PNG, JPG/JPEG, WEBP, HEIC/HEIF.) into one of the categories below using zero-shot CLIP classification (no training/dataset needed):

- **indoors**: castle&fort&tower, cave, church, crypt, dungeon, inn&house, manor, modern, scifi, unclassified
- **outdoors**: camp, castle&fort&tower, overgrowth, planar, regional, ruin, scifi, ship, site of interest, trail&bridge&travel, urban&town&city, village&rural, water&coast, wilderness
- **other**: map scenes, unclassified (for matches below the confience threshold, by default this is 25%), not a map (only with `--detect-photos`)

The maps are then MOVED into category-named subfolders.  Apple Photos images are exported as classified copies into that destination (originals are left alone in Photos) and also get the category written back as a Photos keyword on the original.

It only scans files sitting directly in the selected folder, not subdirectories so re-running it won't re-classify maps a previous run already sorted into category subfolders.

*Undoing a sort.* Every run that moves files writes a record of what it moved (`fgu_moves_<date>_<time>.jsonl`) in the destination folder. To put everything back where it was, including the `.xml` sidecars: `python fgu_classify_maps.py --undo "<that file>"`. It shows what it will do and asks first, never overwrites a file that has since appeared at the original place (it leaves that map where it is and says so), and never removes a folder, so category folders it empties stay. Apple Photos exports are copies and aren't covered; the originals were never moved.

*A report instead of guessing.* `--report results.csv` writes one row per map: its file, the tag, how confident the model was, and what happened to it. It works with `--no-move`, which makes a dry run you can read in a spreadsheet before deciding to move anything. It never overwrites a report that already exists; a second one is saved as `results (2).csv`.

*Speed.* The category descriptions are now encoded once per run and the maps go through the model in batches (`--batch-size`, default 16). The answers are the same as the one-at-a-time method (`fgu_support_test_classifier.py` checks that); on a CPU it measured about 3.8 times faster. That was measured on a CPU only. On an Apple Silicon GPU it has not been timed, so the speed-up there is unknown, though the answers are checked to be the same.

*Photos in the pile (experimental, off by default).* `--detect-photos` also asks the model whether each image is a photograph rather than a map, and tags those `not a map` instead of forcing them into a map category; `--photo-threshold` (default 80) is how sure it has to be. It is untuned: on a small test, drawn maps were never flagged, but only some real photos were (scenic landscapes can read as maps to the model). Look at the `--report` output before trusting it with a real library.

`fgu_classify_maps.py`:

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
| `--batch-size N` | `16` | Maps per model pass; lower it if memory is tight |
| `--report FILE.csv` | | Write a CSV of every map's tag, confidence and outcome (never overwrites) |
| `--detect-photos` | off | Tag images that look like photographs `not a map` (experimental) |
| `--photo-threshold` | `80.0` | Confidence (%) needed before `--detect-photos` tags something `not a map` |
| `--undo MANIFEST` | | Put back everything a previous run moved, using its `fgu_moves_*.jsonl` record |


**fgu_find_maps.py**

Finds maps by describing them ("tavern interior at night", "cave with an underground river"), and shows a whole library as a page of pictures. It looks at the images themselves, not their file names or folders, so it also finds maps that sit in `unclassified`, `map scenes` or a folder you named yourself. It is the same CLIP model the classifier uses. It runs on both Mac OS X and Windows, with a double-click launcher on each (below) or from the command line.

*Before you start*

Run the classifier launcher once (`run_classifier_map.command` on a Mac, `run_classifier_map.bat` on Windows). That downloads the CLIP model, which this tool needs. This tool never goes online, so it can't download the model itself; if the model is missing it says so and stops. If you don't want to sort anything yet, leave the destination blank in the classifier's dialog: it then only classifies, and moves nothing, but it still downloads the model.

*The easy way: the launcher*

Double-click `run_find_maps.command` (Mac) or `run_find_maps.bat` (Windows). The first run sets up Python and the packages if they aren't there yet, exactly as the other launchers do (on Windows it shares the classifier's setup, so there is nothing more to install after running the classifier). Then:

1. Read the short explanation and click Continue.
2. Pick your maps folder.
3. Choose **Search**, type what you are looking for (for example `tavern interior at night`), and click OK. Or choose **Whole Library** to get a page of the entire library grouped by folder.
4. The first time, it reads every map once; you'll see its progress in the terminal window. When it finishes, the picture page opens in your web browser. Click a thumbnail to open the real map.

*The command line*

For more control (such as `--top`), open a terminal in this folder and use the Python in the `venv`:
- Mac: `source venv/bin/activate`, then run the commands below as written.
- Windows: put `venv\Scripts\python.exe` where the commands below say `python`.

*Using it from the command line*

1. **Search.** Point it at your map library (the top folder; sub-folders are included, so it works on a library the classifier has already sorted) and say what you want:

   ```
   python fgu_find_maps.py search --dir "/path/to/maps" "tavern interior at night"
   ```

   The first time, it reads every map once to learn what it looks like. That is the slow part and scales with the number of maps and their size. For scale: a 4,300-map, 9 GB library took about 10 minutes on an Apple M1 Pro (roughly 7 maps a second), and most of that was decoding the images rather than the AI model. Other computers, and computers without a fast GPU, will be slower, and it has not been timed on Windows. After that a search takes a few seconds.

It should never look stuck. It says when it is loading the model (a few seconds, before anything else appears), then prints a line after every batch with the percent done, the speed and an estimate of the time left. A very large map is announced before it is read (the biggest ones take a few seconds each), and if more than 5 seconds pass without a line it prints "still working". It saves as it goes, so if you stop it and run it again it carries on rather than starting over. The window shows the newest of these lines next to the job's title while it runs.
2. **Open the picture page.** When it finishes it prints the path of an HTML page, something like `.../maps/.fgu_map_index/search_tavern-interior-at-night_2026-11-01_114716.html`, and lists the best matches in the terminal. Open that page in a browser, or add `--open` to the command to have it open for you. You get a grid of thumbnails, best match first, with the folder and a similarity score under each. Click a thumbnail to open the real map file.
3. **Use the map.** From there, copy it (and its `.xml`) into Fantasy Grounds as described under **Getting a map into Fantasy Grounds** below.

To just look through everything, `sheet` writes pages for the whole library. It never puts the whole library on one page: you get an index page that lists each folder with its map count and links to that folder's pages, and every page holds at most 96 maps (change it with `--per-page`, up to 500). The pages go in a new `sheet_<date>_<time>` folder inside `.fgu_map_index`, and the command prints which file to open first (`index.html`). If you would rather click through a library than open files, the `fgu_ui.py` window has a Browse tab that does the same thing.

```
python fgu_find_maps.py sheet --dir "/path/to/maps"
```

| Command / flag | Default | Purpose |
| --- | --- | --- |
| `search --dir DIR "words"` | | Rank the library against a description and write a picture page |
| `sheet --dir DIR` | | Write picture pages for the whole library: an index of folders, then pages of at most 96 maps |
| `--per-page N` | `96` | (`sheet`) Most maps on one page (1 to 500) |
| `index --dir DIR` | | Only update the index (see below); writes no page |
| `--top N` | `24` | (`search`) How many results to show (at most 500) |
| `--open` | off | (`search`, `sheet`) Open the page in your browser when it is done |
| `--batch-size N` | `16` | Maps per model pass while reading; lower it if memory is tight |

*Writing good searches*

- Describe the place and what is in it, not the file: "frozen lake with a small fishing hut", "ship deck at sea", "sewer tunnels". The tool already wraps your words in "a top-down battle map of ...", so there is no need to say that.
- The scores are only useful for ordering. In testing on a library of about 4,300 maps they sat in a narrow band (around 0.31 to 0.36) even for good matches, so don't read anything into the exact number or look for a cut-off. Judge by the thumbnails.
- If the first wording misses, try another ("inn", "tavern", "bar room"). Distinctive features (a cave, a ship) worked better than generic ones ("a room").
- CLIP is a general-purpose model, not one trained on battle maps. Treat the page as a shortlist to look through, not a guarantee.

*Keeping the index up to date*

You normally don't need to do anything: `search` and `sheet` bring the index up to date themselves before they run, reading only maps that are new or have changed since last time. A map that was merely moved to another folder (which is what the classifier does when it sorts) is recognised and isn't read again, so sorting a library doesn't mean re-reading it. A file it can't read is skipped, named in the output, and not retried until it changes. Use `index` on its own only when you want to do the slow first pass ahead of time, for example overnight.

*What it writes, and how to reset it*

Everything it writes goes into a hidden `.fgu_map_index` folder inside the library: the index (`index.npz`, updated in place), a small JPEG thumbnail per map in `thumbs/`, and the HTML pages. In total that came to about 17 KB per map on a 4,300-map library (73 MB), almost all of it thumbnails. Your maps are only ever read. A page is never overwritten (a new one gets the next free name), nothing is ever deleted, and it does not touch the `.xml` files. Because nothing is deleted, pages pile up with each search and thumbnails of maps you later remove stay behind. The folder is only a cache, so you can delete old pages, or the whole `.fgu_map_index` folder, by hand at any time; the next run simply rebuilds what is missing. Very large print-size maps are supported (see **Very large maps** below).

**fgu_detect_grid.py**

Detects if a grid exists (square grids only supported) on each image, and if it exists, creates a fantasy grounds compatible xml file with the grid pattern.

Unlike `fgu_classify_maps.py`, this scans recursively (including category subfolders), so it can reach maps already sorted into them by `fgu_classify_maps.py`. 

`fgu_detect_grid.py`:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--dir DIR` | | Local folder of map images |
| `--album ALBUM` | | Apple Photos album name |
| `-f`, `--force` | off | Overwrite an existing sidecar XML instead of skipping it |
| `--grid-min-px` | `20` | Smallest grid square (px) to consider |
| `--grid-max-fraction` | `0.25` | Largest grid square, as a fraction of the shorter image side |
| `--grid-min-confidence` | `0.15` | Minimum autocorrelation confidence (0-1) before reporting "no grid" |
| `--grid-min-line-coverage` | `0.5` | Minimum fraction (0-1) of the image a candidate grid line must actually span to count as real |

**Running on Windows**

Windows has neither Python nor bash, so there are Windows launchers that set everything up for you: double-click `run_ui.bat` (a window for all of the tools below), `run_classifier_map.bat` (sorts maps into categories), `run_grid_detector.bat` (grid detection) or `run_find_maps.bat` (find maps by description). They show the same dialogs as the Mac `.command` launchers. Both just start `run_windows.ps1`, which is where the real work is.

The first run needs an internet connection and takes a few minutes. It downloads [uv](https://github.com/astral-sh/uv) (a pinned version, checked against a SHA-256 hash in `run_windows.ps1` before it is ever run) and uses it to fetch a private copy of Python 3.12 and the packages listed in `requirements-classify.txt` or `requirements-grid.txt`. Everything goes into the `windows_tools` and `venv` folders next to the scripts: nothing is installed system-wide, no admin rights are needed, and deleting those two folders removes it all. Keep the folder path short (for example `C:\fgu`): some of the packages have very long file names, and a deep path can push them over Windows' 260-character limit.

After that first run the launchers make no network requests of their own. The CLIP model update check in `fgu_classify_maps.py` is the only thing that goes online, at most once every 24 hours (see `--no-update-check`).

To do the setup ahead of time (for example before a trip with no internet), run `powershell -ExecutionPolicy Bypass -File run_windows.ps1 -Tool classify -SetupOnly`, and the same again with `-Tool grid`. That installs everything and stops, without opening any dialog.

Differences from the Mac:

- No Apple Photos albums (`--album`); point it at a folder.
- CPU only, so classification is slower than on an Apple Silicon Mac.
- The `.bat` files start PowerShell with `-ExecutionPolicy Bypass` for that one run, because Windows blocks `.ps1` files by default. It doesn't change any system setting.

To run a script directly instead, after the launcher has run once: `venv\Scripts\python.exe fgu_classify_maps.py --dir "C:\Maps"`.

Status: CI tests the setup and the scripts on a real Windows machine on every push: the first-run install, the fact that a second run does no setup, and the safety tests. The dialogs and the double-click `.bat` files are not tested, because CI can't click through them; they have only been reviewed by hand.


**Package versions**

`requirements-classify.txt` and `requirements-grid.txt` list the packages the tools need, as version ranges. The Mac launchers, the Windows launcher and CI all install from these same two files, so the versions CI tests are the versions you get. Each lower bound is a version that has been tested; each upper bound stops the next major release from being picked up until it has been tested. An existing Mac `venv` is not upgraded as long as it already satisfies the ranges. (`osxphotos<0.67`, which only the Mac `--album` mode uses, is added by the Mac launchers themselves.) On Windows, editing a range re-runs the install once.


**Very large maps**

Pillow, the image library all three tools use, refuses images over about 179 million pixels as protection against "decompression bomb" files. Print-size maps go past that (a 60 x 60 inch map at 300 ppi is 324 million pixels), so the tools raise the limit on purpose: images up to 700 million pixels are opened, and anything larger is still refused and reported by name. The classifier and `fgu_find_maps.py` shrink very large JPEGs while decoding them, so those cost little; a large PNG or WebP has to be decoded in full, which takes memory (about 1 GB for 324 million pixels). The grid detector can't shrink the image, because grid spacing is measured in real pixels, and needs about 3.6 GB at 324 million pixels (measured: 17 seconds). `fgu_find_maps.py` retries a map it couldn't read whenever this limit changes.


**Getting a map into Fantasy Grounds**

Neither `fgu_classify_maps.py` nor `fgu_detect_grid.py` puts anything into Fantasy Grounds by itself as library can run into the thousands of files. To move the maps over:

1. Copy the map image and if it exists also the matching `.xml` (same base filename, e.g. `map.png` + `map.xml`) into your campaign's `images/` folder (flat, not in a subfolder).
2. In Fantasy Grounds: sidebar -> Library -> Assets -> Images, then click **Refresh Folder Assets** (bottom right) so it notices the new files. That same panel has a link (bottom right) that opens the images folder in Finder (File Explorer on Windows), handy for copying files over.
3. Click the image, then click **Create Image Record**.
4. In the opened map, click **Grid**, then click the eye icon to make the grid visible.



*Support scripts*

These scripts are for supporting purposes. You probably don't need to run these.

**fgu_support_telemetry.py**

This script captures Mac OS X system telemetry to help analysing map performance. It was used to generate the heuristics on map performance used in fgu_support_maptest.py

**fgu_support_telemetry_analyze.py**

This analyses the telemetry captured by fgu_support_telemetry.py

**fgu_support_test_safety.py**

Safety tests for `fgu_classify_maps.py`, `fgu_find_maps.py` and `fgu_detect_grid.py`: checks that none of them does anything destructive beyond what's explicitly allowed (moving maps into category subfolders and undoing that, writing a CSV report, creating/overwriting a map's `.xml` sidecar, writing the `.fgu_map_index` cache), and that none ever overwrites an existing file without that being explicitly opted into (`-y` for moving, `-f`/`--force` for sidecar overwrite). It also checks that undo restores files byte-for-byte and never overwrites, and that `fgu_find_maps.py` only reads the maps. Every write-capable call either script uses is intercepted for the duration of each run, and every path touched is checked against the exact directories that run was told to use Each script runs in-process so that interception can see every call; nothing about the scripts' own logic is mocked. A second, independent check snapshots the whole repo before and after and fails if anything in it changed at all. Only `--dir` mode is covered; `--album` mode's Photos-keyword write and osxphotos' own export aren't (no safe way to test against a real Photos library), and every `fgu_classify_maps.py` invocation passes `--no-update-check` so its model-update check is never exercised by these tests (CI exercises it separately, see below). Run from inside the project `venv`:

```
source venv/bin/activate
python3 fgu_support_test_safety.py
```

The very first run will be slow while `fgu_classify_maps.py` downloads the CLIP model; it's cached after that, and every test here passes `--no-update-check` so none of them re-trigger a freshness check against huggingface.co.




**fgu_support_test_classifier.py**

Correctness tests (the safety tests cover what the scripts do to files; these cover whether the model code still gives the right answers). It checks that the batched, prompts-encoded-once method in `fgu_classify_maps.py` gives the same probabilities and the same winning category as the original one-image-at-a-time method, whatever the batch size; that an unreadable file costs only itself; that `fgu_find_maps.py` encodes text and images exactly as the classifier does; and that a search for a colour ranks the image of that colour first. It uses the real CLIP model from the local cache and never goes online (run the classifier once first). Run from inside the project `venv`:

```
source venv/bin/activate
python3 fgu_support_test_classifier.py
```

**fgu_support_test_ui.py**

Tests for the map tools window. It checks who can talk to it (the one-time address, this machine only, same-origin requests only); that it can never put a whole library on screen, which it proves against an index of 5,000 maps (48 per page, 100 search results, 500 folder names, a 3,000-line log); that it only serves the index's own thumbnails and only opens maps the index lists; that each button starts exactly the right command and that sorting, undoing and replacing grid files need the confirmation; that Sort and Grids can be pointed at one chosen folder (and refuse one that isn't there) and that Check runs on a campaign; that it writes nothing itself; and that Index, Search, Sort preview, Sort and Undo work end to end on a small real library (undo restores every map byte for byte). It uses the real CLIP model from the local cache and never goes online. Run from inside the project `venv`:

```
source venv/bin/activate
python3 fgu_support_test_ui.py
```

**fgu_support_check_repo.py**

Quick consistency checks that need nothing installed and take about a second: every script the README mentions exists, each launcher runs a script that exists, the `.command` and `.sh` files are executable, `run_windows.ps1` is pure ASCII, the `.bat` files keep their CRLF line endings, every requirements file that is named exists and lists only bounded version ranges, and every script parses. Run it with `python3 fgu_support_check_repo.py`. It only reads files.

**Continuous integration**

`.github/workflows/ci.yml` runs on every push to any branch (so a branch has a result before you open a pull request, and the checks then show on it): the repo checks above, the safety tests, the classifier correctness tests and the map-tools window tests on macOS and on Windows, and a check of the model-update path against the real Hugging Face (which every other test skips) on both. The Windows job sets itself up with the real launcher (`run_windows.ps1 -SetupOnly`), so it also exercises the first-run download, hash check and install, and fails if a second run does any setup again. The CLIP model is cached between runs. CI can't click through the Windows dialogs, so those still need a human.
