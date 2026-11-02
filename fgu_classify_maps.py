import os
import sys
import csv
import math
import hmac
import hashlib
import secrets
import argparse
import subprocess
import shutil
import json
import time
try:
    import torch
    import pillow_heif
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor
except ImportError as e:
    sys.exit(f"\u274c The Python package '{e.name}' is missing, so this is probably not the project's Python.\n"
             "   Run it with the Python the launchers set up: on a Mac, first `source venv/bin/activate`; on Windows, use\n"
             "   `venv\\Scripts\\python.exe`. Or just start it from its launcher (run_ui.command / run_ui.bat), which sets everything up.")

# This script's own logic is OS-agnostic (os.path/os.walk work cross-platform);
# the only Mac-specific piece is the Apple Photos/AppleScript integration
# (--album), since Photos.app and osxphotos are Mac-only. On Windows it's started
# by run_classifier_map.bat -> run_windows.ps1, which handles installing Python
# and the packages; --album is refused with a clear message there (see main()).

pillow_heif.register_heif_opener()  # lets PIL open .heic/.heif (iPhone's default photo format)

# Pillow refuses any image over twice its MAX_IMAGE_PIXELS (about 179 million
# pixels by default) as protection against decompression bombs. Real print-size
# maps are bigger than that -- a 60 x 60 inch map at 300 ppi is 324 million -- so
# the ceiling is raised on purpose, not removed: nothing over 700 million pixels
# (about 2 GB once decoded) is opened, and Pillow still warns above 350 million.
Image.MAX_IMAGE_PIXELS = 350_000_000
# A JPEG bigger than this is decoded at a reduced size (the model only sees 224 px
# anyway), which is far faster and lighter. Smaller images are decoded in full, as before.
HUGE_IMAGE_PIXELS = 60_000_000

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
parser.add_argument("--batch-size", type=int, default=16,
                     help="How many images to run through the model at once (default: 16). "
                          "Raise it on a machine with plenty of memory; lower it if memory is tight.")
parser.add_argument("--report", type=str, default=None, metavar="FILE.csv",
                     help="Write a CSV of what each image was tagged (and where it went). Combined "
                          "with --no-move this is a dry run: you can review the tags before "
                          "anything moves. An existing file is never overwritten; the next free "
                          "name is used instead.")
parser.add_argument("--trust-manifest", action="store_true",
                     help="With --undo: also accept an undo file that was NOT made by this installation "
                          "(for example one you made on another computer). It shows every move and always "
                          "asks first; it can't be combined with -y.")
parser.add_argument("--detect-photos", action="store_true",
                     help="Experimental: also flag images that look like photographs rather than "
                          "map art, and tag them 'not a map'. Off by default; check a --report "
                          "first, since it hasn't been tuned against a real library.")
parser.add_argument("--photo-threshold", type=float, default=80.0,
                     help="With --detect-photos: minimum percent confidence that an image is a "
                          "photo before it is tagged 'not a map' (default: 80.0)")
parser.add_argument("--undo", type=str, default=None, metavar="MANIFEST",
                     help="Move every file listed in a fgu_moves_*.jsonl manifest (written "
                          "whenever a run moves files) back to where it came from. Never "
                          "overwrites, and leaves emptied category folders in place.")
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
    Matches the convention fgu_support_maptest.py's scan_sidecars() and the uvtt2fgu
    reference tool both expect. Duplicated from fgu_detect_grid.py so the two scripts
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
    sidecar XML (written by fgu_detect_grid.py) if one exists, so the sidecar doesn't end
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

# --- CLIP ENCODING ---
# Text and images are encoded through CLIPModel's long-standing submodules
# (text_model / vision_model and their *_projection layers) rather than
# get_text_features() / get_image_features(): the return type of those has
# changed between transformers releases (4.x vs 5.x differ), while the
# submodules and their pooled outputs have not. The maths is exactly what
# CLIPModel.forward does, so the probabilities are the same as before; the
# difference is that the category prompts are now encoded once per run instead
# of once per image, and images go through the model in batches.

def _pooled(output):
    """The pooled embedding from a CLIP text/vision submodule output (a ModelOutput or a plain tuple)."""
    pooled = getattr(output, "pooler_output", None)
    return pooled if pooled is not None else output[1]

def encode_text_features(model, processor, prompts):
    """L2-normalised text embeddings, one row per prompt."""
    inputs = processor(text=prompts, return_tensors="pt", padding=True).to(DEVICE)
    with torch.no_grad():
        out = model.text_model(input_ids=inputs["input_ids"], attention_mask=inputs["attention_mask"])
        features = model.text_projection(_pooled(out))
    return features / features.norm(dim=-1, keepdim=True)

def encode_image_features(model, pixel_values):
    """L2-normalised image embeddings, one row per image in the batch."""
    with torch.no_grad():
        out = model.vision_model(pixel_values=pixel_values.to(DEVICE))
        features = model.visual_projection(_pooled(out))
    return features / features.norm(dim=-1, keepdim=True)

def softmax_probs(model, image_features, text_features):
    """Per image, the probability of each prompt (rows sum to 1)."""
    with torch.no_grad():
        return (model.logit_scale.exp() * image_features @ text_features.T).softmax(dim=1)

# --- IMAGE PREPARATION ---
# Preparing an image for the model (resizing and normalising it) takes more time per
# map than decoding the file or running the model. transformers' "fast" image processor
# does it about 3x quicker with the same results (embeddings agree to a cosine of
# 0.99998), but it needs torchvision, so it is used when it works and the standard
# processor is used when it doesn't.

SHRINK_TARGET_PIXELS = 4_000_000   # a huge map is box-shrunk to about this many pixels first

def fast_processor_kwargs():
    """How to ask transformers for its fast (torchvision) image processor. In 4.x that is
    use_fast=True; in 5.x use_fast is deprecated and the fast one is simply the default
    whenever torchvision is installed."""
    import transformers
    return {} if int(transformers.__version__.split(".")[0]) >= 5 else {"use_fast": True}

def _processor_attempts():
    # the fast one first, then ways of asking for the standard one (the flag is deprecated
    # in 5.x and will go, so the newer spelling is tried too, and plain defaults last)
    return [fast_processor_kwargs(), {"use_fast": False}, {"backend": "pil"}, {}]

def load_processor(model_id, local_files_only):
    """The CLIP processor, with the fast image processor if it works here, else the standard
    one. Never fails just because the fast one isn't available."""
    attempts = _processor_attempts()
    for number, kwargs in enumerate(attempts):
        try:
            return CLIPProcessor.from_pretrained(model_id, local_files_only=local_files_only, **kwargs)
        except Exception as e:
            if number == 0 and not isinstance(e, (ImportError, OSError)):
                print(f"(Fast image processing isn't available here ({e.__class__.__name__}); using the standard one.)")
            if number == len(attempts) - 1:
                raise

def shrink_if_huge(image):
    """A map over HUGE_IMAGE_PIXELS is box-shrunk to a few megapixels before the model's own
    resize to 224 px. The result is the same to the model, but it keeps the processor from
    turning a 300-megapixel map into gigabytes of floating-point numbers."""
    pixels = image.width * image.height
    if pixels <= HUGE_IMAGE_PIXELS:
        return image
    return image.reduce(math.isqrt(pixels // SHRINK_TARGET_PIXELS) + 1)

def preprocess_image(processor, path):
    """The model-ready 1x3x224x224 tensor for one image file. Each image is decoded,
    converted and shrunk on its own, so a huge map never sits in memory alongside
    the rest of its batch -- only the small tensors are batched. The file is closed
    before this returns (on Windows an open file can't be moved)."""
    with Image.open(path) as opened:
        if opened.width * opened.height > HUGE_IMAGE_PIXELS:
            opened.draft("RGB", (1024, 1024))   # only JPEG can; for other formats this does nothing
        image = shrink_if_huge(opened.convert("RGB"))
    return processor(images=image, return_tensors="pt")["pixel_values"]


# --- OPTIONAL PHOTO CHECK (--detect-photos) ---
# A two-way zero-shot test run on the same image embedding. Experimental: the
# thresholds are not tuned against a real library, so it is opt-in.
PHOTO_PROMPTS = [
    "an illustrated top-down map for a tabletop role-playing game",
    "a photograph of a real-world scene, person, animal or object",
]
NOT_A_MAP_TAG = "not a map"


# --- REPORT + MOVE MANIFEST ---
class _Report:
    """Incremental CSV of what each image was tagged, flushed row by row so an
    interrupted run still leaves a usable file. Never overwrites an existing file."""
    FIELDS = ["file", "tag", "confidence_percent", "photo_probability_percent", "moved_to", "status"]

    def __init__(self, path):
        self.path = _unique_path(os.path.abspath(path))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._file = open(self.path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS)
        self._writer.writeheader()
        self._file.flush()
        print(f"📝 Writing report to {self.path}")

    def add(self, **row):
        self._writer.writerow({k: row.get(k, "") for k in self.FIELDS})
        self._file.flush()

    def close(self):
        self._file.close()

# An undo file says "move this file from here to there", so a forged one (say, shipped
# inside a downloaded map pack) could be used to move any file anywhere. Every record is
# therefore signed with a random key that lives in this installation's venv folder, never
# next to the maps, and --undo only trusts records it can verify.
MANIFEST_KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", ".manifest_key")

def _manifest_key(create=False):
    """The 32-byte signing key, or None if there isn't one (and create is False) or it
    can't be written."""
    try:
        with open(MANIFEST_KEY_PATH, "rb") as f:
            key = f.read()
        if len(key) == 32:
            return key
    except OSError:
        pass
    if not create:
        return None
    try:
        os.makedirs(os.path.dirname(MANIFEST_KEY_PATH), exist_ok=True)
        key = secrets.token_bytes(32)
        with open(MANIFEST_KEY_PATH, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
            f.write(key)
        return key
    except FileExistsError:
        return _manifest_key(create=False)
    except OSError:
        return None

def _record_signature(key, record):
    message = json.dumps([record["from"], record["to"], bool(record.get("sidecar", False))]).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()

def _record_is_authentic(key, record):
    signature = record.get("sig")
    return isinstance(signature, str) and hmac.compare_digest(signature, _record_signature(key, record))


class _MoveManifest:
    """A JSON-lines record of every file moved this run (and whether its .xml
    sidecar went with it), written as each move happens, so --undo can reverse
    them. Only created once the first file actually moves. Each line is signed
    (see _manifest_key) so a record from anywhere else is not trusted."""

    def __init__(self, move_root):
        self.path = _unique_path(os.path.join(os.path.abspath(move_root), time.strftime("fgu_moves_%Y-%m-%d_%H%M%S.jsonl")))
        self._file = None
        self._key = None
        self.count = 0

    def add(self, old_path, new_path, had_sidecar):
        if self._file is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._file = open(self.path, "a", encoding="utf-8")
            self._key = _manifest_key(create=True)
            if self._key is None:
                print("⚠️  Could not create the key that signs the undo file, so --undo will not trust it "
                      "(you would have to use --trust-manifest).")
        record = {"from": os.path.abspath(old_path), "to": os.path.abspath(new_path), "sidecar": bool(had_sidecar)}
        if self._key is not None:
            record["sig"] = _record_signature(self._key, record)
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()
        self.count += 1

    def close(self):
        if self._file is not None:
            self._file.close()


def run_classification(image_paths, is_apple_photos=False, unclassified_threshold=25.0, move_root=None,
                        check_update=False, force_update=False, no_update_check=False,
                        batch_size=16, report_path=None, detect_photos=False, photo_threshold=80.0):
    print("\n--- Category Tagging (CLIP AI) ---")
    model_id = MODEL_ID

    # local_files_only is a genuine per-call argument, unlike HF_HUB_OFFLINE/
    # TRANSFORMERS_OFFLINE -- those are plain module-level constants baked in
    # from os.environ the moment huggingface_hub is first imported (which
    # already happened at the top of this file), so setting them here would
    # silently do nothing. local_files_only is what actually works.
    allow_online = maybe_allow_online_model_check(check_update, force_update, no_update_check)
    model = CLIPModel.from_pretrained(model_id, local_files_only=not allow_online).to(DEVICE)
    processor = load_processor(model_id, not allow_online)

    keywords = list(CATEGORIES.keys())
    text_features = encode_text_features(model, processor, list(CATEGORIES.values()))
    photo_features = encode_text_features(model, processor, PHOTO_PROMPTS) if detect_photos else None

    report = _Report(report_path) if report_path else None
    manifest = _MoveManifest(move_root) if (move_root and not is_apple_photos) else None
    total = len(image_paths)
    updated_paths = []

    try:
        for start in range(0, total, max(1, batch_size)):
            # 1. Load + preprocess each image on its own; one unreadable file
            #    must not take down the rest of its batch.
            entries = []
            for offset, (path, photo_uuid, photo_obj) in enumerate(image_paths[start:start + batch_size]):
                entry = {"idx": start + offset + 1, "path": path, "uuid": photo_uuid, "obj": photo_obj,
                         "pixels": None, "error": None}
                try:
                    entry["pixels"] = preprocess_image(processor, path)
                except Exception as e:
                    entry["error"] = e
                entries.append(entry)

            # 2. One model pass for the whole batch.
            good = [e for e in entries if e["pixels"] is not None]
            if good:
                try:
                    image_features = encode_image_features(model, torch.cat([e["pixels"] for e in good]))
                    probs = softmax_probs(model, image_features, text_features).cpu()
                    photo_probs = softmax_probs(model, image_features, photo_features).cpu() if detect_photos else None
                    for i, e in enumerate(good):
                        e["probs"] = probs[i]
                        e["photo_pct"] = photo_probs[i][1].item() * 100 if detect_photos else None
                except Exception as batch_err:
                    for e in good:
                        e["error"], e["pixels"] = batch_err, None

            # 3. Act on each image in order: tag, write back, move, record.
            for e in entries:
                path, photo_uuid, photo_obj, idx = e["path"], e["uuid"], e["obj"], e["idx"]
                current_path = path
                if e["error"] is not None or e["pixels"] is None:
                    print(f"[{idx}/{total}] ❌ Error: {e['error']}")
                    if report:
                        report.add(file=path, status=f"error: {e['error']}")
                    updated_paths.append((current_path, photo_uuid, photo_obj))
                    continue

                best_idx = int(e["probs"].argmax())
                confidence = float(e["probs"][best_idx]) * 100
                assigned_tag = keywords[best_idx] if confidence >= unclassified_threshold else "unclassified"
                detail = f"{confidence:.1f}%"
                if detect_photos and e["photo_pct"] >= photo_threshold:
                    assigned_tag, detail = NOT_A_MAP_TAG, f"looks like a photo, {e['photo_pct']:.1f}%"
                print(f"[{idx}/{total}] {os.path.basename(path)} -> Tagged: '{assigned_tag}' ({detail})")

                status, moved_to = "ok", ""
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
                                    moved_to = exported[0]      # a copy; the original stays in Photos
                                    print(f"    -> exported copy to {exported[0]}")
                        else:
                            had_sidecar = os.path.exists(_sidecar_path(path))
                            current_path = moved_to = move_image_and_sidecar(path, dest_dir)
                            manifest.add(path, current_path, had_sidecar)
                            print(f"    -> moved to {current_path}")
                    except Exception as move_err:
                        status = f"move/export failed: {move_err}"
                        print(f"    -> ❌ move/export failed: {move_err}")

                if report:
                    report.add(file=path, tag=assigned_tag, confidence_percent=f"{confidence:.1f}",
                               photo_probability_percent="" if e["photo_pct"] is None else f"{e['photo_pct']:.1f}",
                               moved_to=moved_to, status=status)
                updated_paths.append((current_path, photo_uuid, photo_obj))
    finally:
        if report:
            report.close()
        if manifest:
            manifest.close()

    if manifest and manifest.count:
        print(f"\n↩️  Moved {manifest.count} file(s). To put them all back:\n"
              f"    python fgu_classify_maps.py --undo \"{manifest.path}\"")
    return updated_paths


def undo_moves(manifest_path, assume_yes=False, trust_manifest=False):
    """Move every file listed in a move manifest back to where it came from. Only
    moves: it never overwrites a file (an occupied original location is skipped and
    reported), never deletes anything, and leaves emptied category folders alone.
    Only records signed by this installation are used (see _manifest_key), unless
    trust_manifest is set, which shows every move and always asks first. Returns False
    if it refused or could not read the file, True otherwise (including "declined")."""
    try:
        with open(manifest_path, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        if not all(isinstance(e, dict) and isinstance(e.get("from"), str) and isinstance(e.get("to"), str) for e in entries):
            raise ValueError("a line is not a move record")
    except (OSError, ValueError) as e:
        print(f"❌ Could not read the manifest {manifest_path}: {e}")
        return False

    key = _manifest_key(create=False)
    authentic = key is not None and all(_record_is_authentic(key, e) for e in entries)
    if not authentic:
        if not trust_manifest:
            print("❌ This undo file was not made by this installation (its records are unsigned or "
                  "don't match), so it was NOT used: a forged undo file could move any file on this "
                  "computer.\n   If you made it yourself, for example on another computer, run again "
                  "with --trust-manifest; that shows every move and asks first.")
            return False
        if assume_yes:
            print("❌ --trust-manifest needs you to read the moves and confirm them yourself, so it can't "
                  "be combined with -y.")
            return False
        print("⚠️  Using an undo file this installation did not make. Read the moves below carefully.")
        assume_yes = False

    plan, skipped = [], []
    for entry in reversed(entries):          # newest move first
        now_at, original = entry["to"], entry["from"]
        if not os.path.isfile(now_at):
            skipped.append((now_at, "it is no longer at the place it was moved to"))
        elif os.path.exists(original):
            skipped.append((now_at, f"something already exists at the original location, {original}"))
        else:
            plan.append((now_at, original, entry.get("sidecar", False)))

    print(f"\n↩️  {len(plan)} file(s) can be moved back; {len(skipped)} will be skipped.")
    for now_at, reason in skipped[:10]:
        print(f"    skipping {now_at}\n      because {reason}")
    if len(skipped) > 10:
        print(f"    ... and {len(skipped) - 10} more")
    for now_at, original, _ in plan[:20]:
        print(f"    {now_at}\n      -> {original}")
    if len(plan) > 20:
        print(f"    ... and {len(plan) - 20} more")
    if not plan:
        return True

    if not assume_yes:
        try:
            confirm = input("Proceed? [y/N]: ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm != "y":
            print("Nothing was moved.")
            return True

    restored = 0
    for now_at, original, had_sidecar in plan:
        try:
            os.makedirs(os.path.dirname(original), exist_ok=True)
            shutil.move(now_at, original)
            sidecar_now, sidecar_original = _sidecar_path(now_at), _sidecar_path(original)
            if had_sidecar and os.path.exists(sidecar_now) and not os.path.exists(sidecar_original):
                shutil.move(sidecar_now, sidecar_original)
            restored += 1
        except Exception as e:
            print(f"    ❌ could not move {now_at} back: {e}")
    print(f"✅ Moved {restored} file(s) back. Any category folders left empty were not removed.")
    return True


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
    if args.undo:
        sys.exit(0 if undo_moves(args.undo, args.yes, args.trust_manifest) else 1)
    if args.batch_size < 1:
        print("❌ --batch-size must be at least 1.")
        return

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
        if sys.platform != "darwin":
            print("❌ Apple Photos albums are only supported on macOS. Use --dir with a folder of images instead.")
            return
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
        batch_size=args.batch_size,
        report_path=args.report,
        detect_photos=args.detect_photos,
        photo_threshold=args.photo_threshold,
    )

if __name__ == "__main__":
    main()
