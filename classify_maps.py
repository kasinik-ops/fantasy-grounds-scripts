import os
import argparse
import subprocess
import xml.etree.ElementTree as ET
import cv2
import numpy as np
import torch
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

# --- 1. ARGUMENT PARSER (Receives input from .command GUI) ---
parser = argparse.ArgumentParser(description="TTRPG Map Processor")
parser.add_argument("--dir", type=str, help="Path to local folder containing map images")
parser.add_argument("--album", type=str, help="Name of Apple Photos album")
parser.add_argument("--pass", choices=["1", "2", "both"], default="1", help="Execution pass")
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

# Below this confidence (%), CLIP's best guess is unreliable and the image is tagged
# "unclassified" instead of forcing it into the closest (but likely wrong) category.
UNCLASSIFIED_CONFIDENCE_THRESHOLD = 25.0

# --- PASS 1: CLIP CATEGORIZATION ---
def run_pass_1(image_paths, is_apple_photos=False):
    print("\n--- Running Pass 1: Category Tagging (CLIP AI) ---")
    model_id = "openai/clip-vit-base-patch32"
    model = CLIPModel.from_pretrained(model_id).to(DEVICE)
    processor = CLIPProcessor.from_pretrained(model_id)

    keywords = list(CATEGORIES.keys())
    prompts = list(CATEGORIES.values())

    for idx, (path, photo_uuid) in enumerate(image_paths, start=1):
        try:
            image = Image.open(path).convert("RGB")
            inputs = processor(text=prompts, images=image, return_tensors="pt", padding=True).to(DEVICE)
            
            with torch.no_grad():
                outputs = model(**inputs)
                probs = outputs.logits_per_image.softmax(dim=1)

            best_idx = probs.argmax().item()
            confidence = probs[0][best_idx].item() * 100
            assigned_tag = keywords[best_idx] if confidence >= UNCLASSIFIED_CONFIDENCE_THRESHOLD else "unclassified"

            print(f"[{idx}/{len(image_paths)}] {os.path.basename(path)} -> Tagged: '{assigned_tag}' ({confidence:.1f}%)")

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

        except Exception as e:
            print(f"[{idx}/{len(image_paths)}] ❌ Error: {e}")

# --- PASS 2: FGU GRID DETECTION ---
# Grid spacing is detected from the map's own pixels via autocorrelation of its
# edge profile. <gridsize> is the verified FGU per-image sidecar field (confirmed
# against the Imagix/uvtt2fgu reference implementation). <gridoffset> is a
# best-effort guess -- FGU's per-image sidecar format is only confirmed to
# support <gridsize>; whether it honors an offset tag at all has NOT been
# verified. Test-import one map in FGU and check the grid lines up before
# trusting it across a whole library.
MIN_GRID_PX = 20
MAX_GRID_FRACTION = 0.25  # a grid square can't be wider than this fraction of the shorter image side
GRID_DETECTION_MIN_CONFIDENCE = 0.15  # normalized autocorrelation peak; below this, treat as "no grid"

FGU_XML_VERSION = "4.1"
FGU_XML_DATAVERSION = "20210302"

def _axis_profiles(gray):
    """Column/row projection profiles that spike at vertical/horizontal grid lines."""
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    col_profile = np.abs(grad_x).sum(axis=0)
    row_profile = np.abs(grad_y).sum(axis=1)
    return col_profile, row_profile

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

def _center_offset(phase_px, spacing, extent):
    """Fold a from-edge pixel offset into the signed offset (in [-spacing/2, spacing/2))
    of the nearest grid line from the image's center, matching the pixel-from-center
    convention FGU already uses for occluder/light coordinates in this sidecar format."""
    centered = (phase_px - extent / 2) % spacing
    if centered >= spacing / 2:
        centered -= spacing
    return centered

def detect_grid(image_path):
    """Detect (spacing_x, spacing_y, offset_x, offset_y, confidence) in pixels, or
    None if no reliable grid is found."""
    img = cv2.imread(image_path)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape

    max_px = int(min(width, height) * MAX_GRID_FRACTION)
    col_profile, row_profile = _axis_profiles(gray)
    spacing_x, conf_x = _dominant_period(col_profile, MIN_GRID_PX, max_px)
    spacing_y, conf_y = _dominant_period(row_profile, MIN_GRID_PX, max_px)

    if spacing_x is None or spacing_y is None:
        return None
    confidence = min(conf_x, conf_y)
    if confidence < GRID_DETECTION_MIN_CONFIDENCE:
        return None

    offset_x = _center_offset(_grid_phase(col_profile, spacing_x), spacing_x, width)
    offset_y = _center_offset(_grid_phase(row_profile, spacing_y), spacing_y, height)

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

def run_pass_2(image_paths):
    print("\n--- Running Pass 2: Fantasy Grounds Grid Alignment ---")
    detected_any = False
    for idx, (path, _) in enumerate(image_paths, start=1):
        result = detect_grid(path)
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

# --- MAIN CONTROLLER ---
def main():
    image_paths = [] # Holds tuples of (file_path, photo_uuid)
    is_apple_photos = False

    if getattr(args, 'dir') and args.dir:
        folder = args.dir
        valid_exts = ('.png', '.jpg', '.jpeg', '.webp')
        for root, _, files in os.walk(folder):
            for file in files:
                if file.lower().endswith(valid_exts):
                    image_paths.append((os.path.join(root, file), None))
        print(f"📂 Found {len(image_paths)} map files in local directory.")

    elif getattr(args, 'album') and args.album:
        import osxphotos
        is_apple_photos = True
        photosdb = osxphotos.PhotosDB()
        photos = photosdb.photos(albums=[args.album])
        for p in photos:
            if p.path:
                image_paths.append((p.path, p.uuid))
        print(f"📸 Found {len(image_paths)} map files in Apple Photos album '{args.album}'.")

    if not image_paths:
        print("❌ No valid images found.")
        return

    # Execute selected passes
    selected_pass = getattr(args, 'pass')
    if selected_pass in ["1", "both"]:
        run_pass_1(image_paths, is_apple_photos)
    if selected_pass in ["2", "both"]:
        run_pass_2(image_paths)

if __name__ == "__main__":
    main()
