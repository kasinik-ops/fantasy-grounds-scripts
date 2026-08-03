import os
import argparse
import subprocess
import shutil
import json
import time
import torch
import pillow_heif
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

# Windows support is a planned future feature. This script's own logic is already
# OS-agnostic (os.path/os.walk work cross-platform); the only Mac-specific piece is
# the Apple Photos/AppleScript integration below, which is inherent to that feature
# (Photos.app and osxphotos are Mac-only) rather than something to special-case here.
# A future Windows launcher would just be a separate .bat/.ps1 calling this same
# script -- nothing else to prepare for that now.

pillow_heif.register_heif_opener()  # lets PIL open .heic/.heif (iPhone's default photo format)

VALID_IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif')

# --- ARGUMENT PARSER (Receives input from run_classifier_map.command GUI) ---
parser = argparse.ArgumentParser(description="TTRPG Map Categorizer")
parser.add_argument("--dir", type=str, help="Path to local folder containing map images")
parser.add_argument("--album", type=str, help="Name of Apple Photos album")
parser.add_argument("--unclassified-threshold", type=float, default=25.0,
                     help="Minimum CLIP confidence percent before tagging 'unclassified' "
                          "instead of forcing the closest category (default: 25.0)")
parser.add_argument("--move-to", type=str, default=None,
                     help="Destination directory to sort classified images into "
                          "(a subfolder per category is created inside it). Local folders "
                          "default to the source folder itself if omitted; Apple Photos "
                          "albums prompt for a destination.")
parser.add_argument("--no-move", action="store_true",
                     help="Classify only, don't move/export files into category folders")
parser.add_argument("-y", "--yes", action="store_true",
                     help="Skip the move/export confirmation prompt")
parser.add_argument("--check-update", action="store_true",
                     help="Check Hugging Face for a newer CLIP model now, bypassing the "
                          "24-hour throttle. Prompts before downloading (default: yes).")
parser.add_argument("--force-update", action="store_true",
                     help="Check for a newer CLIP model now and download it without prompting "
                          "if one is found (bypasses the 24-hour throttle).")
parser.add_argument("--no-update-check", action="store_true",
                     help="Never check Hugging Face for a model update this run -- use "
                          "whatever is already cached, fully offline. For limited/no internet.")
args = parser.parse_args()

DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"

CATEGORIES = {
    "indoors, castle&fort&tower": "a top-down TTRPG battle map of the indoor interior of a castle, fort, keep, or tower",
    "indoors, cave": "a top-down TTRPG battle map of an indoor cave or natural cavern",
    "indoors, church": "a top-down TTRPG battle map of an indoor church, cathedral, or temple",
    "indoors, crypt": "a top-down TTRPG battle map of an indoor crypt, tomb, or mausoleum",
    "indoors, dungeon": "a top-down TTRPG battle map of an indoor dungeon or stone prison cells",
    "indoors, inn&house": "a top-down TTRPG battle map of the indoor interior of a tavern, inn, or house",
    "indoors, manor": "a top-down TTRPG battle map of the indoor interior of a manor, mansion, or noble estate",
    "indoors, modern": "a top-down TTRPG battle map of a modern-day indoor building interior, like an office or apartment",
    "indoors, scifi": "a top-down TTRPG battle map of a sci-fi indoor spaceship, space station, or futuristic facility interior",
    "indoors, unclassified": "a top-down TTRPG battle map of an indoor scene that does not fit a specific room type",
    "map scenes": "regional or overview artwork depicting a scene or setting, not a grid-ready top-down battle map",
    "outdoors, camp": "a top-down TTRPG battle map of an outdoor campsite with tents or a campfire",
    "outdoors, castle&fort&tower": "a top-down TTRPG battle map of the outdoor exterior grounds of a castle, fort, or tower",
    "outdoors, overgrowth": "a top-down TTRPG battle map of an outdoor area overgrown with vines, roots, or dense vegetation",
    "outdoors, planar": "a top-down TTRPG battle map of an outdoor otherworldly or extraplanar landscape",
    "outdoors, regional": "a top-down TTRPG battle map showing a wide regional or overworld map",
    "outdoors, ruin": "a top-down TTRPG battle map of outdoor ancient stone ruins",
    "outdoors, scifi": "a top-down TTRPG battle map of an outdoor sci-fi or futuristic environment",
    "outdoors, ship": "a top-down TTRPG battle map of the outdoor deck of a ship or boat",
    "outdoors, site of interest": "a top-down TTRPG battle map of a distinctive outdoor point of interest or landmark",
    "outdoors, trail&bridge&travel": "a top-down TTRPG battle map of an outdoor trail, road, or bridge for travel",
    "outdoors, urban&town&city": "a top-down TTRPG battle map of an outdoor urban town or city street",
    "outdoors, village&rural": "a top-down TTRPG battle map of an outdoor rural village or farmland",
    "outdoors, water&coast": "a top-down TTRPG battle map of an outdoor body of water, coastline, or beach",
    "outdoors, wilderness": "a top-down TTRPG battle map of outdoor wilderness, forest, or untamed nature",
}

def _sidecar_path(image_path):
    """FGU sidecar path for image_path: same directory and base filename, with the
    image's own extension replaced by .xml (map.png -> map.xml) -- NOT map.png.xml.
    Matches the convention fgu_maptest.py's scan_sidecars() and the uvtt2fgu
    reference tool both expect. Duplicated from detect_grid.py so the two scripts
    stay independent rather than sharing a module."""
    return os.path.splitext(image_path)[0] + ".xml"

def _unique_path(dest_path):
    """dest_path if free, otherwise the same path with ' (2)', ' (3)', ... inserted
    before the extension, so a move never silently overwrites an unrelated file."""
    if not os.path.exists(dest_path):
        return dest_path
    base, ext = os.path.splitext(dest_path)
    n = 2
    while True:
        candidate = f"{base} ({n}){ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1

def move_image_and_sidecar(image_path, dest_dir):
    """Move image_path into dest_dir (collision-safe), bringing along its FGU grid
    sidecar XML (written by detect_grid.py) if one exists, so the sidecar doesn't end
    up orphaned pointing at a file that's no longer there. Returns the image's new path."""
    dest_image_path = _unique_path(os.path.join(dest_dir, os.path.basename(image_path)))
    src_sidecar = _sidecar_path(image_path)
    shutil.move(image_path, dest_image_path)
    if os.path.exists(src_sidecar):
        shutil.move(src_sidecar, _sidecar_path(dest_image_path))
    return dest_image_path

def resolve_move_root(args, image_count, is_apple_photos, source_dir):
    """Ask (via CLI flag or an interactive prompt) where classified files should be
    sorted into, then warn and confirm before anything is touched. Returns the
    destination root, or None if moving/exporting should be skipped."""
    if args.no_move:
        return None

    if args.move_to:
        move_root = os.path.abspath(os.path.expanduser(args.move_to))
    else:
        default_hint = os.path.abspath(source_dir) if source_dir else None
        prompt = "Destination directory to sort classified files into (a subfolder per category will be created inside it)"
        prompt += f" [{default_hint}]: " if default_hint else ": "
        try:
            answer = input(prompt).strip()
        except EOFError:
            answer = ""
        if answer:
            move_root = os.path.abspath(os.path.expanduser(answer))
        elif default_hint:
            move_root = default_hint
        else:
            print("❌ No destination directory given; skipping move/export step.")
            return None

    if is_apple_photos:
        verb, detail = "export copies of", "Originals stay untouched in Photos; classified COPIES will be written there."
    else:
        verb, detail = "MOVE", "Files will be moved out of their current location -- this is not easily undone."
    print(f"\n⚠️  This will {verb} {image_count} file(s) into category-named subfolders under:\n    {move_root}\n    {detail}")

    if not args.yes:
        try:
            confirm = input("Proceed? [y/N]: ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm != "y":
            print("Skipping move/export step.")
            return None

    return move_root

# --- CLIP MODEL UPDATE CHECK ---
# This is the only network access this script makes once its pip dependencies
# are installed. By default it's throttled to once per 24h (state persisted in
# UPDATE_STATE_PATH) and, if an update is actually available, asks before
# downloading it -- since transformers' own from_pretrained() would otherwise
# silently hit the network on every single run just to check freshness, which
# is slow to the point of minutes-long retries on a bad connection (see
# --no-update-check for skipping this entirely).
MODEL_ID = "openai/clip-vit-base-patch32"
UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
UPDATE_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", ".model_update_check")
REMOTE_CHECK_TIMEOUT_SECONDS = 5

def _load_last_checked():
    try:
        with open(UPDATE_STATE_PATH) as f:
            return json.load(f).get("last_checked", 0)
    except (OSError, ValueError):
        return 0

def _save_last_checked(timestamp):
    try:
        os.makedirs(os.path.dirname(UPDATE_STATE_PATH), exist_ok=True)
        with open(UPDATE_STATE_PATH, "w") as f:
            json.dump({"last_checked": timestamp}, f)
    except OSError:
        pass  # best-effort -- worst case we just check again next run

def _cached_model_revision():
    """Commit hash of the CLIP model snapshot on the 'main' ref currently in
    the local cache, or None if nothing is cached yet."""
    from huggingface_hub import scan_cache_dir
    try:
        for repo in scan_cache_dir().repos:
            if repo.repo_id == MODEL_ID:
                for revision in repo.revisions:
                    if "main" in revision.refs:
                        return revision.commit_hash
    except Exception:
        pass
    return None

def _remote_model_revision():
    """Commit hash of the CLIP model's current 'main' revision on Hugging
    Face, or None if unreachable. A single bounded attempt (no retry storm --
    unlike from_pretrained's own downloader), so this fails fast on a bad
    connection instead of hanging."""
    from huggingface_hub import HfApi
    try:
        return HfApi().model_info(MODEL_ID, timeout=REMOTE_CHECK_TIMEOUT_SECONDS).sha
    except Exception:
        return None

def maybe_allow_online_model_check(check_update, force_update, no_update_check):
    """Decides, respecting the 24-hour throttle and the CLI override flags,
    whether this run may check Hugging Face for a CLIP model update -- and if
    an update is found, prompts before allowing it (default yes) unless
    --force-update already answered that for us. Returns True if the caller
    should let from_pretrained() reach the network, False if it should be
    forced offline (nothing new to fetch, throttled, skipped, or declined)."""
    if no_update_check:
        return False

    due = (time.time() - _load_last_checked()) >= UPDATE_CHECK_INTERVAL_SECONDS
    if not (check_update or force_update or due):
        return False

    local_rev = _cached_model_revision()
    first_time = local_rev is None
    print("🔎 No cached CLIP model yet -- checking Hugging Face..." if first_time
          else "🔎 Checking Hugging Face for a CLIP model update...")
    remote_rev = _remote_model_revision()
    _save_last_checked(time.time())

    if remote_rev is None:
        if first_time:
            print("   Could not reach Hugging Face, and no model is cached -- this will fail without a connection.")
        else:
            print("   Could not reach Hugging Face -- using the cached model.")
        return False
    if local_rev == remote_rev:
        print("   Model is already up to date.")
        return False

    if not force_update:
        prompt = "   No model is cached yet -- download it now?" if first_time else \
                 "   A newer CLIP model is available -- download it?"
        try:
            answer = input(f"{prompt} [Y/n]: ").strip().lower()
        except EOFError:
            # No terminal to answer from (non-interactive/automated run) --
            # decline rather than silently starting a download. This is NOT
            # the same as a real Enter press, which does default to yes below.
            answer = "n"
        if answer not in ("", "y", "yes"):
            print("   Skipping download." if first_time else "   Skipping update, using the cached model.")
            return False

    print("   Downloading model..." if first_time else "   Downloading updated model...")
    return True

def run_classification(image_paths, is_apple_photos=False, unclassified_threshold=25.0, move_root=None,
                        check_update=False, force_update=False, no_update_check=False):
    print("\n--- Category Tagging (CLIP AI) ---")
    model_id = MODEL_ID

    # local_files_only is a genuine per-call argument, unlike HF_HUB_OFFLINE/
    # TRANSFORMERS_OFFLINE -- those are plain module-level constants baked in
    # from os.environ the moment huggingface_hub is first imported (which
    # already happened at the top of this file), so setting them here would
    # silently do nothing. local_files_only is what actually works.
    allow_online = maybe_allow_online_model_check(check_update, force_update, no_update_check)
    model = CLIPModel.from_pretrained(model_id, local_files_only=not allow_online).to(DEVICE)
    processor = CLIPProcessor.from_pretrained(model_id, local_files_only=not allow_online)

    keywords = list(CATEGORIES.keys())
    prompts = list(CATEGORIES.values())
    updated_paths = []

    for idx, (path, photo_uuid, photo_obj) in enumerate(image_paths, start=1):
        current_path = path
        try:
            image = Image.open(current_path).convert("RGB")
            inputs = processor(text=prompts, images=image, return_tensors="pt", padding=True).to(DEVICE)

            with torch.no_grad():
                outputs = model(**inputs)
                probs = outputs.logits_per_image.softmax(dim=1)

            best_idx = probs.argmax().item()
            confidence = probs[0][best_idx].item() * 100
            assigned_tag = keywords[best_idx] if confidence >= unclassified_threshold else "unclassified"

            print(f"[{idx}/{len(image_paths)}] {os.path.basename(current_path)} -> Tagged: '{assigned_tag}' ({confidence:.1f}%)")

            if is_apple_photos and photo_uuid:
                # Write back to Apple Photos
                script = f'''
                tell application "Photos"
                    set targetPhoto to media item id "{photo_uuid}"
                    set currentKeywords to keywords of targetPhoto
                    if currentKeywords is missing value then set currentKeywords to {{}}
                    if currentKeywords does not contain "{assigned_tag}" then
                        copy "{assigned_tag}" to end of currentKeywords
                        set keywords of targetPhoto to currentKeywords
                    end if
                end tell
                '''
                subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL)

            if move_root:
                dest_dir = os.path.join(move_root, assigned_tag)
                os.makedirs(dest_dir, exist_ok=True)
                try:
                    if is_apple_photos:
                        if photo_obj is not None:
                            exported = photo_obj.export(dest_dir)
                            if exported:
                                print(f"    -> exported copy to {exported[0]}")
                    else:
                        current_path = move_image_and_sidecar(current_path, dest_dir)
                        print(f"    -> moved to {current_path}")
                except Exception as move_err:
                    print(f"    -> ❌ move/export failed: {move_err}")

        except Exception as e:
            print(f"[{idx}/{len(image_paths)}] ❌ Error: {e}")

        updated_paths.append((current_path, photo_uuid, photo_obj))

    return updated_paths

def _scan_local_dir(folder):
    """[(path, None, None), ...] for supported image files sitting directly inside
    folder. Deliberately non-recursive: subdirectories are assumed to be category
    folders a previous run already sorted, and shouldn't be re-classified."""
    paths = []
    for file in sorted(os.listdir(folder)):
        full_path = os.path.join(folder, file)
        if os.path.isfile(full_path) and file.lower().endswith(VALID_IMAGE_EXTS):
            paths.append((full_path, None, None))
    return paths

def _scan_album(album_name):
    """[(path, uuid, PhotoInfo), ...] for supported image items in the named Apple
    Photos album."""
    import osxphotos
    photosdb = osxphotos.PhotosDB()
    photos = photosdb.photos(albums=[album_name])
    paths = []
    skipped = 0
    for p in photos:
        if p.path and p.path.lower().endswith(VALID_IMAGE_EXTS):
            paths.append((p.path, p.uuid, p))
        elif p.path:
            skipped += 1  # e.g. videos/Live Photo .mov components -- not a still image
    if skipped:
        print(f"   (skipped {skipped} non-image item(s), e.g. videos)")
    return paths

def main():
    using_dir = bool(getattr(args, 'dir') and args.dir)
    using_album = bool(getattr(args, 'album') and args.album)
    is_apple_photos = using_album

    if not using_dir and not using_album:
        print("❌ No valid images found.")
        return

    if using_dir:
        image_paths = _scan_local_dir(args.dir)
        print(f"📂 Found {len(image_paths)} map files directly in local directory.")
    else:
        image_paths = _scan_album(args.album)
        print(f"📸 Found {len(image_paths)} map files in Apple Photos album '{args.album}'.")

    if not image_paths:
        print("❌ No valid images found.")
        return

    move_root = resolve_move_root(args, len(image_paths), is_apple_photos, args.dir)
    run_classification(
        image_paths, is_apple_photos,
        unclassified_threshold=args.unclassified_threshold,
        move_root=move_root,
        check_update=args.check_update,
        force_update=args.force_update,
        no_update_check=args.no_update_check,
    )

if __name__ == "__main__":
    main()
