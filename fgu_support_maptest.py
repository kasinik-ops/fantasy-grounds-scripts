#!/usr/bin/env python3
"""
fgu_mapload.py — Fantasy Grounds Unity map load analyzer.

Statically scans a campaign for the three things that drive per-client
render load on maps with dynamic lighting / line-of-sight:

  1. LoS occluder segment count   (the LoS engine's main cost)
  2. Dynamic light count + area   (each 'on' light is ~a render pass)
  3. Map pixel dimensions + bytes (texture VRAM + network push)

It reads occluders/lights from db.xml image records AND from same-named
.xml sidecar files in the images folder, pairs them with the actual image
file to read width x height and size on disk, then ranks maps worst-first.

Zero dependencies — standard library only. Image dimensions are read from
file headers directly (PNG/JPEG/GIF/BMP/WEBP), so Pillow is NOT required.

The thresholds below are HEURISTIC starting points using a cumulative point 
system, not official FGU numbers. The *ranking* (which maps are heaviest 
relative to the rest) is the trustworthy part. Tune with the CLI flags.
"""
from __future__ import annotations
import argparse, csv, os, struct, sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")

# ---- cumulative performance weights (all overridable via CLI) -------------
# Each map's colour = its TOTAL cumulative score. 
# This reflects the multiplicative strain of Lights + LoS + Resolution on the engine.
DEF = dict(
    # WEIGHTS (Points per unit of strain)
    weight_mp = 1.0,           # 1 point per 1 Megapixel
    weight_mb = 2.0,           # 2 points per 1 Megabyte (MB)
    weight_los_100 = 3.0,      # 3 points per 100 LoS segments
    weight_light = 3.0,        # 3 points per light source
    
    # PENALTIES
    los_penalty_limit = 1000,  # Threshold for dense geometry penalty
    los_penalty_points = 15.0, # Flat point penalty if crossing the LoS limit
    
    # CUMULATIVE SCORE THRESHOLDS (Final map rating)
    # Green is < 31
    score_yellow = 31,         # 31-65: Standard Play (Minor load times)
    score_orange = 66,         # 66-110: Heavy Load (Token drag stutter)
    score_red = 111,           # 111+: Danger Zone (Severe lag/disconnect risk)
)

# --------------------------------------------------------------------------
# Image header dimension readers (no external libs)
# --------------------------------------------------------------------------
def _png(f):
    f.seek(0)
    if f.read(8) != b"\x89PNG\r\n\x1a\n":
        return None
    f.seek(16)
    w, h = struct.unpack(">II", f.read(8))
    return w, h

def _gif(f):
    f.seek(0)
    if f.read(6) not in (b"GIF87a", b"GIF89a"):
        return None
    w, h = struct.unpack("<HH", f.read(4))
    return w, h

def _bmp(f):
    f.seek(0)
    if f.read(2) != b"BM":
        return None
    f.seek(18)
    w, h = struct.unpack("<ii", f.read(8))
    return abs(w), abs(h)

def _jpeg(f):
    f.seek(0)
    if f.read(2) != b"\xff\xd8":
        return None
    while True:
        b = f.read(1)
        if not b:
            return None
        if b != b"\xff":
            continue
        marker = f.read(1)
        while marker == b"\xff":            # skip fill bytes
            marker = f.read(1)
        m = marker[0]
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            continue
        seg = f.read(2)
        if len(seg) < 2:
            return None
        (length,) = struct.unpack(">H", seg)
        # SOF markers carry the frame dimensions
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            f.read(1)                       # precision
            h, w = struct.unpack(">HH", f.read(4))
            return w, h
        f.seek(length - 2, os.SEEK_CUR)

def _webp(f):
    f.seek(0)
    hdr = f.read(12)
    if len(hdr) < 12 or hdr[0:4] != b"RIFF" or hdr[8:12] != b"WEBP":
        return None
    fourcc = f.read(4)
    if fourcc == b"VP8 ":
        f.seek(26); data = f.read(4)
        w = struct.unpack("<H", data[0:2])[0] & 0x3FFF
        h = struct.unpack("<H", data[2:4])[0] & 0x3FFF
        return w, h
    if fourcc == b"VP8L":
        f.seek(21); b = f.read(4)
        n = struct.unpack("<I", b)[0]
        w = (n & 0x3FFF) + 1
        h = ((n >> 14) & 0x3FFF) + 1
        return w, h
    if fourcc == b"VP8X":
        f.seek(24); d = f.read(6)
        w = (d[0] | d[1] << 8 | d[2] << 16) + 1
        h = (d[3] | d[4] << 8 | d[5] << 16) + 1
        return w, h
    return None

def image_dims(path):
    """Return (width, height) in pixels, or None if unreadable."""
    try:
        with open(path, "rb") as f:
            ext = os.path.splitext(path)[1].lower()
            order = {".png": [_png], ".gif": [_gif], ".bmp": [_bmp],
                     ".jpg": [_jpeg], ".jpeg": [_jpeg], ".webp": [_webp]}
            for reader in order.get(ext, []) + [_png, _jpeg, _gif, _bmp, _webp]:
                try:
                    f.seek(0)
                    r = reader(f)
                    if r and r[0] and r[1]:
                        return r
                except Exception:
                    continue
    except OSError:
        return None
    return None

# --------------------------------------------------------------------------
# Occluder / light parsing (shared by db.xml records and sidecar files)
# --------------------------------------------------------------------------
def _floats(text):
    out = []
    for tok in (text or "").replace(";", ",").split(","):
        tok = tok.strip()
        if tok:
            try:
                out.append(float(tok))
            except ValueError:
                pass
    return out

def occluder_segments(occ_el):
    """Segments in one <occluder>: pairs-1, +1 more if <closed/>."""
    pts_el = occ_el.find("points")
    nums = _floats(pts_el.text) if pts_el is not None else _floats(occ_el.findtext("points"))
    pairs = len(nums) // 2
    segs = max(pairs - 1, 0)
    if occ_el.find("closed") is not None and pairs >= 3:
        segs += 1
    return segs

def light_radius(light_el):
    """Outer (dim) radius in grid units from <range> 'b,bf,d,df'."""
    nums = _floats(light_el.findtext("range"))
    if len(nums) >= 3:
        return nums[2]
    return nums[0] if nums else 0.0

@dataclass
class MapRec:
    name: str
    source: str                 # 'db.xml' or 'sidecar'
    image_path: str | None = None
    occ_count: int = 0
    seg_total: int = 0
    doors: int = 0              # toggleable occluders
    light_count: int = 0
    light_on: int = 0
    light_area: float = 0.0     # sum pi*r^2 (grid^2), 'on' lights only
    width: int | None = None
    height: int | None = None
    nbytes: int | None = None

    @property
    def mp(self):
        if self.width and self.height:
            return self.width * self.height / 1_000_000
        return None

def collect_los(el, rec: MapRec):
    for occ in el.iter("occluder"):
        rec.occ_count += 1
        rec.seg_total += occluder_segments(occ)
        if occ.find("toggleable") is not None:
            rec.doors += 1
    for lt in el.iter("light"):
        rec.light_count += 1
        on = lt.find("on") is not None
        if on:
            rec.light_on += 1
            r = light_radius(lt)
            rec.light_area += 3.14159 * r * r

# --------------------------------------------------------------------------
# db.xml scan
# --------------------------------------------------------------------------
def parent_map(root):
    return {c: p for p in root.iter() for c in p}

def nearest_name(el, parents):
    cur = el
    while cur is not None:
        nm = cur.find("name")
        if nm is not None and (nm.text or "").strip():
            return nm.text.strip()
        cur = parents.get(cur)
    return None

def find_bitmap(el, campaign_dir):
    """First descendant text that resolves to an image file on disk."""
    cands = []
    for sub in el.iter():
        t = (sub.text or "").strip()
        if t.lower().endswith(IMAGE_EXTS):
            cands.append(t)
    for c in cands:
        rel = c.replace("\\", "/")
        for base in (campaign_dir, os.path.dirname(campaign_dir)):
            p = os.path.normpath(os.path.join(base, rel))
            if os.path.isfile(p):
                return p
        # bare filename fallback: look in images/
        p = os.path.join(campaign_dir, "images", os.path.basename(rel))
        if os.path.isfile(p):
            return p
    return cands[0] if cands else None

def image_record_of(el, parents):
    """The map RECORD owning this occluder/light: the nearest ancestor that
    holds an <image> data block (i.e. has a direct child named 'image').
    This is what merges a map's layers ('Base', 'Lights', ...) into one map
    instead of treating each named layer as a separate record."""
    cur = parents.get(el)
    while cur is not None:
        if cur.find("image") is not None:
            return cur
        cur = parents.get(cur)
    return None

def record_name(rec_el):
    nm = rec_el.find("name")
    if nm is not None and (nm.text or "").strip():
        return nm.text.strip()
    return None

def scan_db(db_path, campaign_dir, inspect=False):
    recs = []
    try:
        tree = ET.parse(db_path)
    except ET.ParseError as e:
        print(f"  ! could not parse {db_path}: {e}", file=sys.stderr)
        return recs
    root = tree.getroot()
    parents = parent_map(root)

    # One record per map. A map's occluders/lights may be spread across image
    # layers; attribute them all to the enclosing image record.
    owners = {}
    for tag in ("occluder", "light"):
        for el in root.iter(tag):
            owner = image_record_of(el, parents)
            if owner is None:                      # fallback: nearest named ancestor
                hop = parents.get(el)
                while hop is not None and hop.find("name") is None:
                    hop = parents.get(hop)
                owner = hop
            if owner is not None:
                owners.setdefault(id(owner), owner)

    if inspect and owners:
        first = next(iter(owners.values()))
        print("\n--- INSPECT: structure of first map record in db.xml ---")
        _dump(first, 0)
        print("--- end inspect ---\n")

    for owner in owners.values():
        name = record_name(owner) or nearest_name(owner, parents) or "(unnamed)"
        rec = MapRec(name=name, source="db.xml")
        collect_los(owner, rec)
        bmp = find_bitmap(owner, campaign_dir)
        if bmp and os.path.isfile(bmp):
            rec.image_path = bmp
            rec.nbytes = os.path.getsize(bmp)
            d = image_dims(bmp)
            if d:
                rec.width, rec.height = d
        elif bmp:
            rec.image_path = bmp + "  (file not found on disk)"
        recs.append(rec)
    return recs

def _dump(el, depth, max_depth=4, max_children=6):
    pad = "  " * depth
    txt = (el.text or "").strip()
    txt = f" = {txt[:40]}" if txt else ""
    print(f"{pad}<{el.tag}>{txt}")
    if depth >= max_depth:
        return
    for i, c in enumerate(el):
        if i >= max_children:
            print(f"{pad}  ...")
            break
        _dump(c, depth + 1, max_depth, max_children)

# --------------------------------------------------------------------------
# images-folder sidecar scan
# --------------------------------------------------------------------------
def scan_sidecars(images_dir, covered_paths):
    recs = []
    if not os.path.isdir(images_dir):
        return recs
    for entry in sorted(os.listdir(images_dir)):
        if not entry.lower().endswith(".xml"):
            continue
        stem = os.path.splitext(entry)[0]
        img = None
        for ext in IMAGE_EXTS:
            cand = os.path.join(images_dir, stem + ext)
            if os.path.isfile(cand):
                img = cand
                break
        if img and os.path.normpath(img) in covered_paths:
            continue  # already represented by a db.xml record
        xml_path = os.path.join(images_dir, entry)
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            continue
        if root.find(".//occluder") is None and root.find(".//light") is None:
            continue
        rec = MapRec(name=stem, source="sidecar")
        collect_los(root, rec)
        if img:
            rec.image_path = img
            rec.nbytes = os.path.getsize(img)
            d = image_dims(img)
            if d:
                rec.width, rec.height = d
        else:
            rec.image_path = "(sidecar .xml with no matching image)"
        recs.append(rec)
    return recs

# --------------------------------------------------------------------------
# campaign-level signals: combat-tracker effects + db.xml size
# --------------------------------------------------------------------------
@dataclass
class CampSignals:
    db_bytes: int = 0
    combatants: int = 0
    effects: int = 0
    light_effects: int = 0

LIGHT_HINTS = ("light", "torch", "lantern", "candle", "lamp", "daylight")

def scan_ct(root):
    """Count combat-tracker combatants, active effects, and light-emitting
    effects (the token-movement freeze trigger). Tolerant to tag nesting."""
    combatants = effects = light_effects = 0
    # effects live under one or more <effects> containers; each child record
    # carries a <label>. Count records; flag those whose label reads as a light.
    for eff_box in root.iter("effects"):
        for rec in list(eff_box):
            lab = rec.find("label")
            if lab is None:
                lab = rec.find(".//label")
            effects += 1
            txt = (lab.text if lab is not None else "") or ""
            if any(h in txt.lower() for h in LIGHT_HINTS):
                light_effects += 1
    for ct in root.iter():
        if ct.tag in ("combattracker", "ct"):
            lst = ct.find("list")
            if lst is not None:
                combatants += len(list(lst))
            break
    return combatants, effects, light_effects

def campaign_signals(db_path):
    sig = CampSignals()
    if not os.path.isfile(db_path):
        return sig
    sig.db_bytes = os.path.getsize(db_path)
    try:
        root = ET.parse(db_path).getroot()
        sig.combatants, sig.effects, sig.light_effects = scan_ct(root)
    except ET.ParseError:
        pass
    return sig

# --------------------------------------------------------------------------
# scoring: tiered colour bands (green < yellow < orange < red)
# --------------------------------------------------------------------------
GREEN, YELLOW, ORANGE, RED = 0, 1, 2, 3
LEVEL_NAME = {GREEN: "GREEN", YELLOW: "YELLOW", ORANGE: "ORANGE", RED: "RED"}
LEVEL_256 = {GREEN: 46, YELLOW: 226, ORANGE: 208, RED: 196}

def classify(rec, th):
    """Calculates cumulative map strain and returns a color rating, score breakdown, and recommendations."""
    pts = {}
    recommendations = []
    
    # 1. Resolution Strain (GPU)
    mp = rec.mp or 0.0
    pts["px"] = mp * th["weight_mp"]
    if pts["px"] >= 20:
        recommendations.append("Downscale image to <= 4000x4000 pixels in an external editor before importing.")
    
    # 2. Network Strain (File Size)
    mb = (rec.nbytes or 0) / (1024 * 1024)
    pts["bytes"] = mb * th["weight_mb"]
    if pts["bytes"] >= 20:
        recommendations.append("Compress image or convert to WEBP format to reduce file size below 10MB.")
    
    # 3. Geometry Strain (CPU)
    pts["seg"] = (rec.seg_total / 100.0) * th["weight_los_100"]
    if rec.seg_total > th["los_penalty_limit"]:
        pts["seg"] += th["los_penalty_points"]
    if pts["seg"] >= 20:
        recommendations.append("Use FG's 'Simplify' tool on LoS lines (Unlock Map > Line of Sight > Select lines > Simplify).")
        
    # 4. Lighting Strain (CPU/GPU Multiplier)
    pts["lgt"] = rec.light_count * th["weight_light"]
    if pts["lgt"] >= 20:
        recommendations.append("Reduce light count. Remove overlapping/redundant lights or switch to Global Ambient Lighting.")
    
    # Evaluate final cumulative score
    total_score = sum(pts.values())
    
    if total_score >= th["score_red"]:
        overall = RED
    elif total_score >= th["score_orange"]:
        overall = ORANGE
    elif total_score >= th["score_yellow"]:
        overall = YELLOW
    else:
        overall = GREEN
        
    if overall >= ORANGE and not recommendations:
        recommendations.append("Minor loads across multiple elements are stacking up. Optimize the heaviest element.")
        
    return overall, pts, total_score, recommendations

# ANSI colouring (auto-off when not a TTY; --color/--no-color override)
_USE_COLOR = False
def paint(text, level):
    if not _USE_COLOR:
        return text
    return f"\033[1;38;5;{LEVEL_256[level]}m{text}\033[0m"

def enable_windows_ansi():
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass

def human_bytes(n):
    if n is None:
        return "     ?"
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:5.1f}{unit}"
        n /= 1024

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def default_data_dirs():
    c = []
    ap = os.environ.get("APPDATA")
    if ap:
        c.append(os.path.join(ap, "SmiteWorks", "Fantasy Grounds"))
    home = os.path.expanduser("~")
    c.append(os.path.join(home, ".smiteworks", "fgdata"))
    c.append(os.path.join(home, "Library", "Application Support",
                          "SmiteWorks", "Fantasy Grounds"))  # some macOS installs
    return [p for p in c if os.path.isdir(p)]

def resolve_campaign(args):
    """Return list of (campaign_name, campaign_dir)."""
    if args.campaign:
        p = os.path.abspath(args.campaign)
        if os.path.isfile(p) and os.path.basename(p) == "db.xml":
            p = os.path.dirname(p)
        return [(os.path.basename(p.rstrip("/\\")), p)]
    out = []
    dds = [args.data] if args.data else default_data_dirs()
    for dd in dds:
        camp_root = os.path.join(dd, "campaigns")
        if not os.path.isdir(camp_root):
            continue
        for nm in sorted(os.listdir(camp_root)):
            cd = os.path.join(camp_root, nm)
            if os.path.isfile(os.path.join(cd, "db.xml")):
                out.append((nm, cd))
    return out

def main():
    ap = argparse.ArgumentParser(
        description="Rank Fantasy Grounds Unity maps by LoS/lighting/size render load.")
    ap.add_argument("--campaign", help="path to a campaign folder (or its db.xml)")
    ap.add_argument("--data", help="FGU data dir (contains 'campaigns'); "
                                   "auto-detected if omitted")
    ap.add_argument("--csv", help="write full results to this CSV path")
    ap.add_argument("--top", type=int, default=0, help="only print N worst maps")
    ap.add_argument("--inspect", action="store_true",
                    help="dump the XML shape of the first map record (schema check)")
    ap.add_argument("--color", dest="color", action="store_true", default=None,
                    help="force ANSI colour output")
    ap.add_argument("--no-color", dest="color", action="store_false",
                    help="disable ANSI colour output")
    for k, v in DEF.items():
        ap.add_argument(f"--{k}", type=type(v), default=v)
    args = ap.parse_args()
    th = {k: getattr(args, k) for k in DEF}

    global _USE_COLOR
    _USE_COLOR = sys.stdout.isatty() if args.color is None else args.color
    if _USE_COLOR and os.name == "nt":
        enable_windows_ansi()

    camps = resolve_campaign(args)
    if not camps:
        print("No campaign found. Pass --campaign <folder> or --data <fgdata dir>.\n"
              "Tip: the FGU data dir is shown in the Launcher settings.", file=sys.stderr)
        sys.exit(2)

    all_recs = []
    camp_sig = {}
    for name, cdir in camps:
        db = os.path.join(cdir, "db.xml")
        recs = scan_db(db, cdir, inspect=args.inspect) if os.path.isfile(db) else []
        covered = {os.path.normpath(r.image_path) for r in recs
                   if r.image_path and os.path.isfile(r.image_path)}
        recs += scan_sidecars(os.path.join(cdir, "images"), covered)
        for r in recs:
            r.campaign = name
        all_recs += recs
        camp_sig[name] = campaign_signals(db)

    for r in all_recs:
        r._level, r._pts, r._score, r._recs = classify(r, th)
    all_recs.sort(key=lambda r: r._score, reverse=True)

    print("=" * 104)
    print("FGU MAP LOAD REPORT — each map is scored by its CUMULATIVE STATS")
    print(f"campaigns scanned: {', '.join(sorted({r.campaign for r in all_recs})) or '-'}")
    print(f"maps with LoS/lighting data: {len(all_recs)}")
    legend = "  ".join(paint(LEVEL_NAME[l], l) for l in (GREEN, YELLOW, ORANGE, RED))
    print(f"bands (fine -> worst): {legend}")
    print(f"score thresholds: YELLOW {th['score_yellow']} | ORANGE {th['score_orange']} | RED {th['score_red']}")
    print("=" * 104)
    hdr = (f"{'SEVERITY':8} {'MAP':26} {'DIMS':>12} {'MP':>6} {'SIZE':>7} "
           f"{'SEGS':>6} {'LGT':>4}  SCORE (px lgt seg byt)")
    print(hdr)
    print("-" * 104)

    rows = all_recs if args.top <= 0 else all_recs[:args.top]
    for r in rows:
        dims = f"{r.width}x{r.height}" if r.width else "?"
        mp = f"{r.mp:5.1f}" if r.mp else "    ?"
        sev = paint(f"{LEVEL_NAME[r._level]:8}", r._level)
        name = paint(f"{r.name[:26]:26}", r._level)
        # format the points contributed by each axis
        axline = f"{r._pts['px']:>3.0f} {r._pts['lgt']:>3.0f} {r._pts['seg']:>3.0f} {r._pts['bytes']:>3.0f}"
        print(f"{sev} {name} {dims:>12} {mp:>6} "
              f"{human_bytes(r.nbytes):>7} {r.seg_total:>6} {r.light_count:>4}  {r._score:>5.0f} ({axline})")

    print("-" * 104)
    counts = {l: sum(1 for r in all_recs if r._level == l) for l in (RED, ORANGE, YELLOW, GREEN)}
    tally = "   ".join(paint(f"{LEVEL_NAME[l]}: {counts[l]}", l)
                       for l in (RED, ORANGE, YELLOW, GREEN))
    print("band tally   " + tally)

    flagged = [r for r in all_recs if r._level >= ORANGE]
    if flagged:
        print("\nWORST OFFENDERS (start here):")
        for r in flagged[:5]:
            why = []
            if r._pts["px"] >= 20:
                why.append(f"{r._pts['px']:.0f} pts from Resolution ({r.width}x{r.height})")
            if r._pts["lgt"] >= 20:
                why.append(f"{r._pts['lgt']:.0f} pts from Lights ({r.light_count} lights)")
            if r._pts["seg"] >= 20:
                why.append(f"{r._pts['seg']:.0f} pts from LoS ({r.seg_total} segments)")
            if r._pts["bytes"] >= 20:
                why.append(f"{r._pts['bytes']:.0f} pts from Size ({human_bytes(r.nbytes)})")
            
            print("  • " + paint(r.name, r._level) + ": " + ", ".join(why))
            for rec_text in r._recs:
                print(f"      -> Action: {rec_text}")
    else:
        print("\nNo maps reached orange/red. Nothing here is a likely bottleneck.")

    if any(s.db_bytes for s in camp_sig.values()):
        print("\nCAMPAIGN-LEVEL SIGNALS (hit every client, not just one map):")
        for name, s in camp_sig.items():
            db_mb = s.db_bytes / (1024 * 1024)
            big_db = s.db_bytes >= 15 * 1024 * 1024
            db_txt = f"  [{name}] db.xml {db_mb:.1f} MB"
            if big_db:
                db_txt += "  " + paint("<- large: autosave hitches all clients; "
                                       "check /console save time", ORANGE)
            print(db_txt)
            le = f"    combat tracker: {s.combatants} combatants, {s.effects} active effects, "
            le += (paint(f"{s.light_effects} light-emitting <- token-movement freeze trigger", RED)
                   if s.light_effects else f"{s.light_effects} light-emitting")
            print(le)

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["campaign", "map", "source", "severity",
                        "width", "height", "megapixels", "bytes",
                        "occluders", "segments", "doors", "lights", "lights_on",
                        "light_area_grid2", "pts_px", "pts_lights",
                        "pts_seg", "pts_bytes", "total_score", "image_path", "recommendations"])
            for r in all_recs:
                rec_string = " | ".join(r._recs)
                w.writerow([r.campaign, r.name, r.source, LEVEL_NAME[r._level],
                            r.width, r.height, f"{r.mp:.2f}" if r.mp else "", r.nbytes,
                            r.occ_count, r.seg_total, r.doors, r.light_count, r.light_on,
                            f"{r.light_area:.0f}", f"{r._pts['px']:.1f}",
                            f"{r._pts['lgt']:.1f}", f"{r._pts['seg']:.1f}",
                            f"{r._pts['bytes']:.1f}", f"{r._score:.1f}", r.image_path or "", rec_string])
        print(f"\nCSV written: {args.csv}")

if __name__ == "__main__":
    main()
