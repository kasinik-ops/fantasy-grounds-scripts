import os
import argparse
import xml.etree.ElementTree as ET
import cv2
import numpy as np
import pillow_heif
from PIL import Image

# Windows support is a planned future feature. This script's own logic is already
# OS-agnostic (os.path/os.walk work cross-platform) -- a future Windows launcher
# would just be a separate .bat/.ps1 calling this same script, nothing else to
# prepare for that now.

pillow_heif.register_heif_opener()  # lets PIL open .heic/.heif (iPhone's default photo format)

VALID_IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif')

# --- ARGUMENT PARSER (Receives input from run_grid_detector.command GUI) ---
parser = argparse.ArgumentParser(description="TTRPG Map Grid Detector")
parser.add_argument("--dir", type=str, help="Path to local folder containing map images")
parser.add_argument("--album", type=str, help="Name of Apple Photos album")
parser.add_argument("-f", "--force", action="store_true",
                     help="Overwrite existing FGU grid sidecar XML files instead of skipping them")
parser.add_argument("--grid-min-px", type=int, default=20,
                     help="Minimum grid square size to consider, in pixels (default: 20)")
parser.add_argument("--grid-max-fraction", type=float, default=0.25,
                     help="Maximum grid square size, as a fraction of the shorter image "
                          "side (default: 0.25)")
parser.add_argument("--grid-min-confidence", type=float, default=0.15,
                     help="Minimum normalized autocorrelation confidence (0-1) before "
                          "reporting 'no grid detected' (default: 0.15)")
parser.add_argument("--grid-min-line-coverage", type=float, default=0.5,
                     help="Minimum fraction (0-1) of the image a candidate grid line must "
                          "actually span to count as real, rejecting periodic photo texture like "
                          "railings or decking that autocorrelation alone can't tell apart from a "
                          "genuine drawn grid (default: 0.5)")
args = parser.parse_args()

# Grid spacing is detected from the map's own pixels via autocorrelation of its
# edge profile. <gridsize> is the verified FGU per-image sidecar field (confirmed
# against the Imagix/uvtt2fgu reference implementation). <gridoffset> is a
# best-effort guess -- FGU's per-image sidecar format is only confirmed to
# support <gridsize>; whether it honors an offset tag at all has NOT been
# verified. Test-import one map in FGU and check the grid lines up before
# trusting it across a whole library.
FGU_XML_VERSION = "4.1"
FGU_XML_DATAVERSION = "20210302"

def _sidecar_path(image_path):
    """FGU sidecar path for image_path: same directory and base filename, with the
    image's own extension replaced by .xml (map.png -> map.xml) -- NOT map.png.xml.
    Matches the convention fgu_maptest.py's scan_sidecars() and the uvtt2fgu
    reference tool both expect. Duplicated from classify_maps.py so the two scripts
    stay independent rather than sharing a module."""
    return os.path.splitext(image_path)[0] + ".xml"

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
    xml_path = _sidecar_path(image_path)
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

def run_grid_detection(image_paths, force=False, min_grid_px=20, max_grid_fraction=0.25,
                        min_confidence=0.15, min_line_coverage=0.5):
    print("\n--- Fantasy Grounds Grid Detection ---")
    detected_any = False
    for idx, (path, _, _) in enumerate(image_paths, start=1):
        sidecar = _sidecar_path(path)
        if os.path.exists(sidecar) and not force:
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
            f"(confidence {confidence * 100:.0f}%) -> wrote {os.path.basename(sidecar)}"
        )

    if detected_any:
        print(
            "\n⚠️  <gridoffset> is a best-effort guess -- only <gridsize> is a verified FGU "
            "sidecar field. Test-import one map in FGU and confirm the grid lines up before "
            "trusting the offset across the rest of the library."
        )

def _scan_local_dir(folder):
    """[(path, None, None), ...] for supported image files under folder, recursing
    into subdirectories so maps sorted into category folders by classify_maps.py
    are still found."""
    paths = []
    for root, _, files in os.walk(folder):
        for file in files:
            if file.lower().endswith(VALID_IMAGE_EXTS):
                paths.append((os.path.join(root, file), None, None))
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

    if not using_dir and not using_album:
        print("❌ No valid images found.")
        return

    if using_dir:
        image_paths = _scan_local_dir(args.dir)
        print(f"📂 Found {len(image_paths)} map files (including subfolders) for grid detection.")
    else:
        image_paths = _scan_album(args.album)
        print(f"📸 Found {len(image_paths)} map files in Apple Photos album '{args.album}'.")

    if not image_paths:
        print("❌ No valid images found.")
        return

    run_grid_detection(
        image_paths,
        force=args.force,
        min_grid_px=args.grid_min_px,
        max_grid_fraction=args.grid_max_fraction,
        min_confidence=args.grid_min_confidence,
        min_line_coverage=args.grid_min_line_coverage,
    )

if __name__ == "__main__":
    main()
