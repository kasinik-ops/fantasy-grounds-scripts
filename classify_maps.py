import os
import argparse
import subprocess
import shutil
import xml.etree.ElementTree as ET
import cv2
import numpy as np
import torch
import pillow_heif
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

pillow_heif.register_heif_opener()  # lets PIL open .heic/.heif (iPhone's default photo format)

VALID_IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif')

# --- 1. ARGUMENT PARSER (Receives input from .command GUI) ---
parser = argparse.ArgumentParser(description="TTRPG Map Processor")
parser.add_argument("--dir", type=str, help="Path to local folder containing map images")
parser.add_argument("--album", type=str, help="Name of Apple Photos album")
parser.add_argument("--pass", choices=["1", "2", "both"], default="1", help="Execution pass")
parser.add_argument("-f", "--force", action="store_true",
                     help="Pass 2: overwrite existing FGU grid sidecar XML files instead of skipping them")
parser.add_argument("--unclassified-threshold", type=float, default=25.0,
                     help="Pass 1: minimum CLIP confidence percent before tagging 'unclassified' "
                          "instead of forcing the closest category (default: 25.0)")
parser.add_argument("--grid-min-px", type=int, default=20,
                     help="Pass 2: minimum grid square size to consider, in pixels (default: 20)")
parser.add_argument("--grid-max-fraction", type=float, default=0.25,
                     help="Pass 2: maximum grid square size, as a fraction of the shorter image "
                          "side (default: 0.25)")
parser.add_argument("--grid-min-confidence", type=float, default=0.15,
                     help="Pass 2: minimum normalized autocorrelation confidence (0-1) before "
                          "reporting 'no grid detected' (default: 0.15)")
parser.add_argument("--grid-min-line-coverage", type=float, default=0.5,
                     help="Pass 2: minimum fraction (0-1) of the image a candidate grid line must "
                          "actually span to count as real, rejecting periodic photo texture like "
                          "railings or decking that autocorrelation alone can't tell apart from a "
                          "genuine drawn grid (default: 0.5)")
parser.add_argument("--move-to", type=str, default=None,
                     help="Pass 1: destination directory to sort classified images into "
                          "(a subfolder per category is created inside it). Local folders "
                          "default to the source folder itself if omitted; Apple Photos "
                          "albums prompt for a destination.")
parser.add_argument("--no-move", action="store_true",
                     help="Pass 1: classify only, don't move/export files into category folders")
parser.add_argument("-y", "--yes", action="store_true",
                     help="Pass 1: skip the move/export confirmation prompt")
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

# --- PASS 1: CLIP CATEGORIZATION + SORT-BY-CATEGORY ---
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
    sidecar XML (<image_path>.xml, written by Pass 2) if one exists, so the sidecar
    doesn't end up orphaned pointing at a file that's no longer there. Returns the
    image's new path."""
    dest_image_path = _unique_path(os.path.join(dest_dir, os.path.basename(image_path)))
    src_sidecar = f"{image_path}.xml"
    shutil.move(image_path, dest_image_path)
    if os.path.exists(src_sidecar):
        shutil.move(src_sidecar, f"{dest_image_path}.xml")
    return dest_image_path

def resolve_move_root(args, image_count, is_apple_photos, source_dir):
    """Ask (via CLI flag or an interactive prompt) where classified files should be
    sorted into, then warn and confirm before Pass 1 touches anything. Returns the
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

def run_pass_1(image_paths, is_apple_photos=False, unclassified_threshold=25.0, move_root=None):
    print("\n--- Running Pass 1: Category Tagging (CLIP AI) ---")
    model_id = "openai/clip-vit-base-patch32"
    model = CLIPModel.from_pretrained(model_id).to(DEVICE)
    processor = CLIPProcessor.from_pretrained(model_id)

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

# --- PASS 2: FGU GRID DETECTION ---
# Grid spacing is detected from the map's own pixels via autocorrelation of its
# edge profile. <gridsize> is the verified FGU per-image sidecar field (confirmed
# against the Imagix/uvtt2fgu reference implementation). <gridoffset> is a
# best-effort guess -- FGU's per-image sidecar format is only confirmed to
# support <gridsize>; whether it honors an offset tag at all has NOT been
# verified. Test-import one map in FGU and check the grid lines up before
# trusting it across a whole library.
FGU_XML_VERSION = "4.1"
FGU_XML_DATAVERSION = "20210302"

def _axis_profiles(gray):
    """Column/row projection profiles that spike at vertical/horizontal grid lines,
    plus the underlying per-pixel gradient magnitude maps (used by _line_coverage to
    verify a candidate line is a real edge spanning the image, not just texture)."""
    grad_x = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
    grad_y = np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
    col_profile = grad_x.sum(axis=0)
    row_profile = grad_y.sum(axis=1)
    return col_profile, row_profile, grad_x, grad_y

def _dominant_period(profile, min_px, max_px):
    """Strongest repeating spacing in a 1D profile, preferring the smallest
    period that's nearly as strong as the peak (avoids locking onto a 2x/3x
    harmonic of the true grid spacing). Returns (spacing_px, confidence)."""
    profile = profile.astype(np.float64) - profile.mean()
    n = len(profile)
    max_px = min(max_px, n - 1)
    if max_px <= min_px or not np.any(profile):
        return None, 0.0

    autocorr = np.correlate(profile, profile, mode="full")[n - 1:]
    baseline = autocorr[0] if autocorr[0] != 0 else 1e-9
    autocorr = autocorr / baseline
    window = autocorr[min_px:max_px + 1]
    peak = window.max()
    if peak <= 0:
        return None, 0.0

    threshold = peak * 0.85
    for lag_offset, value in enumerate(window):
        if value >= threshold:
            return lag_offset + min_px, float(peak)
    return int(np.argmax(window)) + min_px, float(peak)

def _grid_phase(profile, spacing):
    """Pixel position (from the array's start) of the strongest grid line, folded into one period."""
    sums = np.zeros(spacing)
    for phase in range(spacing):
        sums[phase] = profile[phase::spacing].sum()
    return int(np.argmax(sums))

def _line_coverage(grad_abs, spacing, phase, axis):
    """Fraction of candidate grid lines (spaced `spacing` apart, starting at `phase`)
    backed by a real edge spanning most of the image -- not just a strong aggregate
    sum, which periodic photographic texture (railings, decking, tiled surfaces,
    portholes) can produce just as easily as a genuine drawn grid line. Autocorrelation
    alone can't tell "there's periodicity somewhere in this row/column sum" apart from
    "there's an actual continuous line here", which is what this checks instead.

    Uses the 40th-percentile line, not the mean, so a few lines obscured by map
    tokens/labels don't sink an otherwise-real grid.

    axis=0: candidate lines are columns (vertical grid lines) of grad_abs.
    axis=1: candidate lines are rows (horizontal grid lines) of grad_abs."""
    threshold = np.percentile(grad_abs, 80)
    if threshold <= 0:
        return 0.0
    edge_mask = grad_abs > threshold

    if axis == 0:
        positions = range(phase, edge_mask.shape[1], spacing)
        coverages = [edge_mask[:, x].mean() for x in positions]
    else:
        positions = range(phase, edge_mask.shape[0], spacing)
        coverages = [edge_mask[y, :].mean() for y in positions]

    if not coverages:
        return 0.0
    coverages.sort()
    return float(coverages[int(len(coverages) * 0.4)])

def _center_offset(phase_px, spacing, extent):
    """Fold a from-edge pixel offset into the signed offset (in [-spacing/2, spacing/2))
    of the nearest grid line from the image's center, matching the pixel-from-center
    convention FGU already uses for occluder/light coordinates in this sidecar format."""
    centered = (phase_px - extent / 2) % spacing
    if centered >= spacing / 2:
        centered -= spacing
    return centered

def detect_grid(image_path, min_grid_px=20, max_grid_fraction=0.25, min_confidence=0.15,
                 min_line_coverage=0.5):
    """Detect (spacing_x, spacing_y, offset_x, offset_y, confidence) in pixels, or
    None if no reliable grid is found.

    min_grid_px: smallest grid square (px) to consider.
    max_grid_fraction: largest grid square, as a fraction of the shorter image side.
    min_confidence: normalized autocorrelation peak below which to report "no grid".
    min_line_coverage: minimum fraction of the image a candidate grid line must
    actually span (see _line_coverage) -- rejects periodic photographic texture
    (railings, decking, tiles) that autocorrelation alone can score as confidently
    as a genuine drawn grid."""
    try:
        gray = np.array(Image.open(image_path).convert("L"))
    except Exception:
        return None
    height, width = gray.shape

    max_px = int(min(width, height) * max_grid_fraction)
    col_profile, row_profile, grad_x, grad_y = _axis_profiles(gray)
    spacing_x, conf_x = _dominant_period(col_profile, min_grid_px, max_px)
    spacing_y, conf_y = _dominant_period(row_profile, min_grid_px, max_px)

    if spacing_x is None or spacing_y is None:
        return None
    confidence = min(conf_x, conf_y)
    if confidence < min_confidence:
        return None

    phase_x = _grid_phase(col_profile, spacing_x)
    phase_y = _grid_phase(row_profile, spacing_y)

    coverage_x = _line_coverage(grad_x, spacing_x, phase_x, axis=0)
    coverage_y = _line_coverage(grad_y, spacing_y, phase_y, axis=1)
    if min(coverage_x, coverage_y) < min_line_coverage:
        return None

    offset_x = _center_offset(phase_x, spacing_x, width)
    offset_y = _center_offset(phase_y, spacing_y, height)

    return spacing_x, spacing_y, offset_x, offset_y, confidence

def write_grid_sidecar(image_path, spacing_x, spacing_y, offset_x, offset_y):
    """Create or update the FGU sidecar XML next to image_path with the detected
    grid. Preserves any existing content (e.g. occluders/lights) already there."""
    xml_path = f"{image_path}.xml"
    root = None
    if os.path.exists(xml_path):
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            root = None
    if root is None:
        root = ET.Element("root", attrib={"version": FGU_XML_VERSION, "dataversion": FGU_XML_DATAVERSION})

    gridsize_el = root.find("gridsize")
    if gridsize_el is None:
        gridsize_el = ET.SubElement(root, "gridsize")
    gridsize_el.text = f"{spacing_x},{spacing_y}"

    gridoffset_el = root.find("gridoffset")
    if gridoffset_el is None:
        gridoffset_el = ET.SubElement(root, "gridoffset")
    gridoffset_el.text = f"{offset_x:.1f},{offset_y:.1f}"

    ET.indent(root, space="    ")
    ET.ElementTree(root).write(xml_path, encoding="UTF-8", xml_declaration=True)

def run_pass_2(image_paths, force=False, min_grid_px=20, max_grid_fraction=0.25, min_confidence=0.15,
               min_line_coverage=0.5):
    print("\n--- Running Pass 2: Fantasy Grounds Grid Alignment ---")
    detected_any = False
    for idx, (path, _, _) in enumerate(image_paths, start=1):
        if os.path.exists(f"{path}.xml") and not force:
            print(f"[{idx}/{len(image_paths)}] {os.path.basename(path)} -> Sidecar already exists, skipped (use -f to overwrite).")
            continue
        result = detect_grid(
            path, min_grid_px=min_grid_px, max_grid_fraction=max_grid_fraction,
            min_confidence=min_confidence, min_line_coverage=min_line_coverage,
        )
        if result is None:
            print(f"[{idx}/{len(image_paths)}] {os.path.basename(path)} -> No grid detected, skipped.")
            continue
        spacing_x, spacing_y, offset_x, offset_y, confidence = result
        write_grid_sidecar(path, spacing_x, spacing_y, offset_x, offset_y)
        detected_any = True
        print(
            f"[{idx}/{len(image_paths)}] {os.path.basename(path)} -> "
            f"grid {spacing_x}x{spacing_y}px, offset ({offset_x:+.1f}, {offset_y:+.1f})px from center "
            f"(confidence {confidence * 100:.0f}%) -> wrote {os.path.basename(path)}.xml"
        )

    if detected_any:
        print(
            "\n⚠️  <gridoffset> is a best-effort guess -- only <gridsize> is a verified FGU "
            "sidecar field. Test-import one map in FGU and confirm the grid lines up before "
            "trusting the offset across the rest of the library."
        )

def _scan_local_dir(folder, recursive):
    """[(path, None, None), ...] for supported image files under folder.

    recursive=False only looks directly inside folder -- used for Pass 1, so maps
    already sorted into category subfolders by a previous run aren't re-classified.
    recursive=True walks all subdirectories -- used for Pass 2, so it can still
    reach maps already sorted into those category subfolders."""
    paths = []
    if recursive:
        for root, _, files in os.walk(folder):
            for file in files:
                if file.lower().endswith(VALID_IMAGE_EXTS):
                    paths.append((os.path.join(root, file), None, None))
    else:
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

# --- MAIN CONTROLLER ---
def main():
    using_dir = bool(getattr(args, 'dir') and args.dir)
    using_album = bool(getattr(args, 'album') and args.album)
    is_apple_photos = using_album

    if not using_dir and not using_album:
        print("❌ No valid images found.")
        return

    selected_pass = getattr(args, 'pass')

    if selected_pass in ["1", "both"]:
        if using_dir:
            image_paths = _scan_local_dir(args.dir, recursive=False)
            print(f"📂 Found {len(image_paths)} map files directly in local directory.")
        else:
            image_paths = _scan_album(args.album)
            print(f"📸 Found {len(image_paths)} map files in Apple Photos album '{args.album}'.")

        if not image_paths:
            print("❌ No valid images found for Pass 1.")
        else:
            move_root = resolve_move_root(args, len(image_paths), is_apple_photos, args.dir)
            run_pass_1(
                image_paths, is_apple_photos,
                unclassified_threshold=args.unclassified_threshold,
                move_root=move_root,
            )

    if selected_pass in ["2", "both"]:
        if using_dir:
            image_paths = _scan_local_dir(args.dir, recursive=True)
            print(f"📂 Found {len(image_paths)} map files (including subfolders) for grid detection.")
        else:
            image_paths = _scan_album(args.album)
            print(f"📸 Found {len(image_paths)} map files in Apple Photos album '{args.album}'.")

        if not image_paths:
            print("❌ No valid images found for Pass 2.")
        else:
            run_pass_2(
                image_paths,
                force=args.force,
                min_grid_px=args.grid_min_px,
                max_grid_fraction=args.grid_max_fraction,
                min_confidence=args.grid_min_confidence,
                min_line_coverage=args.grid_min_line_coverage,
            )

if __name__ == "__main__":
    main()
