#!/usr/bin/env python3
"""
Find maps in a library by describing them, or browse the whole library as a
picture sheet.

    python3 fgu_find_maps.py index  --dir LIBRARY
    python3 fgu_find_maps.py search --dir LIBRARY "tavern interior at night"
    python3 fgu_find_maps.py sheet  --dir LIBRARY

index   Reads every image under LIBRARY (sub-folders included) once, with the same
        CLIP model the classifier uses, and remembers what each one looks like.
        Only new or changed images are read again, so re-running it is quick, and a
        map the classifier has since moved into a category folder is not re-read.
search  Ranks the library against a description and writes an HTML page of
        thumbnails (best match first) that you can open in a browser. Click one to
        open the real map. It brings the index up to date first.
sheet   HTML pages for the whole library, grouped by folder. An index page lists the
        folders with their map counts, and each folder's maps are split across pages
        of at most 96, so no page ever holds the whole library.

What it writes, and where: only inside LIBRARY/.fgu_map_index/ -- the index
(index.npz, updated in place when the library changes), a small JPEG thumbnail
per map (thumbs/), and the HTML pages. Your maps are only ever read. An HTML page
is never overwritten: a new one gets the next free name. Nothing is ever deleted,
so thumbnails of maps you later remove stay behind; the whole folder is a cache
and is safe to delete by hand at any time.

It never goes online: the CLIP model must already be downloaded (running the
classifier once does that).
"""
import argparse
import functools
import hashlib
import html
import json
import math
import os
import pathlib
import re
import sys
import time

import numpy as np
import pillow_heif
import torch
from PIL import Image

pillow_heif.register_heif_opener()

# Pillow refuses any image over twice its MAX_IMAGE_PIXELS (about 179 million
# pixels by default) as protection against decompression bombs. Real print-size
# maps are bigger than that -- a 60 x 60 inch map at 300 ppi is 324 million -- so
# the ceiling is raised on purpose, not removed: nothing over 700 million pixels
# (about 2 GB once decoded) is opened, and Pillow still warns above 350 million.
Image.MAX_IMAGE_PIXELS = 350_000_000
# A JPEG bigger than this is decoded at a reduced size: the model sees 224 px and the
# thumbnail is 256 px, so nothing is lost, and it is far faster and lighter.
HUGE_IMAGE_PIXELS = 60_000_000

MODEL_ID = "openai/clip-vit-base-patch32"
VALID_IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif')
INDEX_DIRNAME = ".fgu_map_index"
INDEX_FILENAME = "index.npz"
INDEX_FORMAT = 1                      # bump if the layout of index.npz ever changes
THUMB_SIZE = 256
SAVE_EVERY_BATCHES = 10               # an interrupted run keeps what it has finished
SHEET_PAGE_SIZE = 96                  # maps per picture page; a library is split across many pages
MAX_SEARCH_RESULTS = 500              # the most a search will put on one page
HEARTBEAT_SECONDS = 5                 # while reading, never stay silent longer than this between maps
ANNOUNCE_LARGE_PIXELS = 200_000_000   # only maps this big are announced before being read (the biggest take a few seconds)

DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"


# --- CLIP ENCODING ---
# The same maths as fgu_classify_maps.py (see its comment on why the submodules
# are used rather than get_*_features); fgu_support_test_classifier.py checks the
# two stay in agreement.

def _pooled(output):
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
    """L2-normalised image embeddings, one row per image."""
    with torch.no_grad():
        out = model.vision_model(pixel_values=pixel_values.to(DEVICE))
        features = model.visual_projection(_pooled(out))
    return features / features.norm(dim=-1, keepdim=True)


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

def load_processor(local_files_only=True):
    """The CLIP processor, with the fast image processor if it works here, else the standard
    one. Never fails just because the fast one isn't available."""
    from transformers import CLIPProcessor
    attempts = _processor_attempts()
    for number, kwargs in enumerate(attempts):
        try:
            return CLIPProcessor.from_pretrained(MODEL_ID, local_files_only=local_files_only, **kwargs)
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


@functools.lru_cache(maxsize=1)
def load_model():
    """The cached CLIP model, never touching the network. None if it isn't cached."""
    from transformers import CLIPModel
    try:
        model = CLIPModel.from_pretrained(MODEL_ID, local_files_only=True).to(DEVICE)
        processor = load_processor(local_files_only=True)
    except Exception as e:
        print(f"❌ The CLIP model isn't downloaded yet ({e}).\n"
              f"   Run the classifier once, with internet, to download it; this tool never goes online.")
        return None
    model.eval()
    return model, processor


# --- LIBRARY + INDEX ---

def index_dir(library):
    return os.path.join(library, INDEX_DIRNAME)


def scan_library(library):
    """Sorted [(relative path with '/' separators, size, mtime_ns)] for every supported
    image under library. Hidden folders (which includes the index folder) are skipped."""
    found = []
    for folder, subdirs, files in os.walk(library):
        subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
        for name in sorted(files):
            if not name.lower().endswith(VALID_IMAGE_EXTS) or name.startswith("."):
                continue
            full = os.path.join(folder, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, library).replace(os.sep, "/")
            found.append((rel, st.st_size, st.st_mtime_ns))
    return found


THUMB_NAME = re.compile(r"^[0-9a-f]{20}\.jpg$")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def is_safe_rel(rel):
    """Is rel a plain relative path to an image, written with '/', that stays inside
    whatever folder it is joined to? An index file is just data sitting in the library
    folder, and one that came with a downloaded map pack could list '../something' or a
    program; entries like that are never used."""
    if not isinstance(rel, str) or not rel or not rel.lower().endswith(VALID_IMAGE_EXTS):
        return False
    if rel.startswith("/") or "\x00" in rel or _DRIVE_PREFIX.match(rel) or (os.sep == "\\" and "\\" in rel):
        return False
    return all(part and not part.startswith(".") for part in rel.split("/"))   # no '', '.', '..' or hidden parts


def load_index(library):
    """{'paths','sizes','mtimes','thumbs','embeddings','failed'} from a previous run, or None.
    'failed' is a set of (path, size, mtime) for files that could not be read. Entries
    whose path or thumbnail name isn't safe (see is_safe_rel) are dropped."""
    path = os.path.join(index_dir(library), INDEX_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            if int(data["format"]) != INDEX_FORMAT or str(data["model_id"]) != MODEL_ID:
                return None
            paths, thumbs = [str(p) for p in data["paths"]], [str(t) for t in data["thumbs"]]
            keep = [i for i in range(len(paths)) if is_safe_rel(paths[i]) and THUMB_NAME.match(thumbs[i])]
            sizes, mtimes = [int(s) for s in data["sizes"]], [int(m) for m in data["mtimes"]]
            return {
                "paths": [paths[i] for i in keep],
                "sizes": [sizes[i] for i in keep],
                "mtimes": [mtimes[i] for i in keep],
                "thumbs": [thumbs[i] for i in keep],
                "embeddings": data["embeddings"].astype(np.float32)[keep],
                # A file that failed under a different size ceiling is worth another try.
                "failed": set(zip((str(p) for p in data["failed_paths"]),
                                  (int(s) for s in data["failed_sizes"]),
                                  (int(m) for m in data["failed_mtimes"])))
                if "read_limit" in data.files and int(data["read_limit"]) == Image.MAX_IMAGE_PIXELS else set(),
            }
    except Exception:
        return None  # unreadable or from something else -- just build it again


def save_index(library, entries, failed=()):
    """entries: [(path, size, mtime_ns, thumb_name, embedding_row)]; failed: [(path,
    size, mtime_ns)] of files that could not be read. Written to a
    temporary file next to the index and moved into place, so a crash mid-write
    can't leave a half-written index behind."""
    folder = index_dir(library)
    os.makedirs(folder, exist_ok=True)
    final = os.path.join(folder, INDEX_FILENAME)
    temporary = os.path.join(folder, INDEX_FILENAME + ".tmp")
    dim = entries[0][4].shape[0] if entries else 0
    with open(temporary, "wb") as f:
        np.savez(
            f,
            format=np.array(INDEX_FORMAT),
            model_id=np.array(MODEL_ID),
            read_limit=np.array(Image.MAX_IMAGE_PIXELS),
            paths=np.array([e[0] for e in entries], dtype=str),
            sizes=np.array([e[1] for e in entries], dtype=np.int64),
            mtimes=np.array([e[2] for e in entries], dtype=np.int64),
            thumbs=np.array([e[3] for e in entries], dtype=str),
            failed_paths=np.array([f[0] for f in failed], dtype=str),
            failed_sizes=np.array([f[1] for f in failed], dtype=np.int64),
            failed_mtimes=np.array([f[2] for f in failed], dtype=np.int64),
            embeddings=np.stack([e[4] for e in entries]).astype(np.float32) if entries
            else np.zeros((0, dim), dtype=np.float32),
        )
    os.replace(temporary, final)


def _thumb_name(rel, size, mtime_ns):
    return hashlib.sha1(f"{rel}|{size}|{mtime_ns}".encode("utf-8")).hexdigest()[:20] + ".jpg"


def _duration(seconds):
    """'40 s', '12 min' or '1 h 20 min'."""
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    return f"{seconds // 3600} h {round(seconds % 3600 / 60)} min"


def read_image(library, rel, thumb_name, processor, on_large=None):
    """The model-ready tensor for one map. The map is only read. The thumbnail is written
    once, and an existing one is never rewritten. on_large(megapixels), if given, is called
    before a very large map is decoded, which can take a while."""
    full = os.path.join(library, *rel.split("/"))
    with Image.open(full) as opened:
        pixels = opened.width * opened.height
        if pixels > ANNOUNCE_LARGE_PIXELS and on_large:
            on_large(pixels / 1e6)
        if pixels > HUGE_IMAGE_PIXELS:
            opened.draft("RGB", (1024, 1024))   # only JPEG can; for other formats this does nothing
        image = shrink_if_huge(opened.convert("RGB"))
    pixels = processor(images=image, return_tensors="pt")["pixel_values"]
    thumb_path = os.path.join(index_dir(library), "thumbs", thumb_name)
    if not os.path.exists(thumb_path):
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        image.thumbnail((THUMB_SIZE, THUMB_SIZE))   # in place: no second full-size copy of a huge map
        image.save(thumb_path, "JPEG", quality=80)
    return pixels


def update_index(library, batch_size=16):
    """Brings the index in line with the library, reading only what is new or changed.
    Returns the finished index as a dict of lists/array (see load_index), or None if
    the model is unavailable and something needed reading."""
    on_disk = scan_library(library)
    previous = load_index(library)

    known = {}        # path -> entry reusable as-is
    by_stat = {}      # (size, mtime) -> entry, so a map that was only moved keeps its embedding
    if previous:
        for i, p in enumerate(previous["paths"]):
            entry = (p, previous["sizes"][i], previous["mtimes"][i], previous["thumbs"][i], previous["embeddings"][i])
            known[p] = entry
            by_stat.setdefault((entry[1], entry[2]), entry)

    present = {rel for rel, _, _ in on_disk}
    thumb_exists = lambda name: os.path.exists(os.path.join(index_dir(library), "thumbs", name))
    already_failed = previous["failed"] if previous else set()
    entries, todo, still_failed = [], [], []
    for rel, size, mtime in on_disk:
        if (rel, size, mtime) in already_failed:
            still_failed.append((rel, size, mtime))      # unchanged since it last failed
            continue
        old = known.get(rel)
        if old and old[1] == size and old[2] == mtime and thumb_exists(old[3]):
            entries.append(old)
            continue
        moved = by_stat.get((size, mtime))
        if moved and moved[0] not in present and thumb_exists(moved[3]):
            entries.append((rel, size, mtime, moved[3], moved[4]))   # same map, new location
            continue
        todo.append((rel, size, mtime))

    reused = len(entries)
    print(f"📚 {len(on_disk)} map(s) in the library: {reused} already indexed, {len(todo)} to read"
          + (f", {len(still_failed)} unreadable (skipped until they change)." if still_failed else "."))
    if todo:
        print("   Loading the AI model (a few seconds)...", flush=True)
        loaded = load_model()
        if loaded is None:
            return None
        model, processor = loaded
        print(f"   Reading maps. A progress line follows every {batch_size}, and anything slow is announced.", flush=True)
        batches_since_save = 0
        started = last_print = time.time()
        for start in range(0, len(todo), batch_size):
            chunk, tensors, kept = todo[start:start + batch_size], [], []
            for position, (rel, size, mtime) in enumerate(chunk, 1):
                name = _thumb_name(rel, size, mtime)
                label = os.path.basename(rel)

                def announce(megapixels, label=label):
                    print(f"   large map ({megapixels:,.0f} megapixels): {label} -- this one can take a while", flush=True)
                try:
                    tensors.append(read_image(library, rel, name, processor, on_large=announce))
                    kept.append((rel, size, mtime, name))
                except Exception as e:
                    still_failed.append((rel, size, mtime))
                    print(f"   ⚠️  couldn't read {rel}: {e}", flush=True)
                if time.time() - last_print >= HEARTBEAT_SECONDS:
                    print(f"   still working: {start + position}/{len(todo)} (last: {label})", flush=True)
                    last_print = time.time()
            if kept:
                features = encode_image_features(model, torch.cat(tensors)).cpu().numpy()
                for item, row in zip(kept, features):
                    entries.append((*item, row))
            done = min(start + batch_size, len(todo))
            elapsed = max(time.time() - started, 1e-6)
            rate = done / elapsed
            line = f"   [{done}/{len(todo)}] {100 * done // len(todo)}% \u00b7 {rate:.1f} maps/s"
            line += f" \u00b7 about {_duration((len(todo) - done) / rate)} left" if done < len(todo) \
                else f" \u00b7 finished in {_duration(elapsed)}"
            print(line, flush=True)
            last_print = time.time()
            batches_since_save += 1
            if batches_since_save >= SAVE_EVERY_BATCHES:
                save_index(library, entries, still_failed)
                batches_since_save = 0

    entries.sort(key=lambda e: e[0])
    if todo or previous is None or [e[0] for e in entries] != previous["paths"] \
            or set(still_failed) != already_failed:
        save_index(library, entries, still_failed)
    return {
        "paths": [e[0] for e in entries],
        "thumbs": [e[3] for e in entries],
        "embeddings": np.stack([e[4] for e in entries]).astype(np.float32) if entries else np.zeros((0, 0), dtype=np.float32),
    }


# --- SEARCH ---

QUERY_TEMPLATES = ["{q}", "a top-down battle map of {q}", "a tabletop role-playing game map: {q}"]


def rank(model, processor, index, query):
    """[(score, position in index)] best match first. Score is cosine similarity."""
    prompts = [t.format(q=query) for t in QUERY_TEMPLATES]
    features = encode_text_features(model, processor, prompts).mean(dim=0)
    features = (features / features.norm()).cpu().numpy()
    scores = index["embeddings"] @ features
    order = np.argsort(-scores)
    return [(float(scores[i]), int(i)) for i in order]


# --- HTML PAGES ---

PAGE_STYLE = """
:root { --bg:#fafaf8; --fg:#1d1d1b; --muted:#6b6b66; --card:#ffffff; --line:#e2e0da; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --fg:#ecebe6; --muted:#9a9990; --card:#21211f; --line:#34332f; } }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg); font:15px/1.4 system-ui,sans-serif; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 10px; }
p.note { color:var(--muted); margin:0 0 18px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
.card a { color:inherit; text-decoration:none; display:block; }
.card img { width:100%; aspect-ratio:1/1; object-fit:cover; display:block; background:var(--line); }
.card .cap { padding:8px 10px; font-size:13px; overflow-wrap:anywhere; }
.card .cap small { display:block; color:var(--muted); }
.nav { margin:0 0 14px; } .nav a, ul.folders a { color:var(--fg); }
ul.folders { list-style:none; padding:0; margin:0; }
ul.folders li { padding:7px 0; border-bottom:1px solid var(--line); }
"""


def _card(library, index, position, score=None, thumbs_prefix="thumbs/"):
    rel = index["paths"][position]
    target = pathlib.Path(os.path.abspath(os.path.join(library, *rel.split("/")))).as_uri()
    folder, _, name = rel.rpartition("/")
    extra = f"<small>{html.escape(folder or '(top level)')}"
    if score is not None:
        extra += f" &middot; match {score:.3f}"
    extra += "</small>"
    return (f'<div class="card"><a href="{html.escape(target, quote=True)}">'
            f'<img loading="lazy" src="{thumbs_prefix}{html.escape(index["thumbs"][position], quote=True)}" alt="">'
            f'<div class="cap">{html.escape(name)}{extra}</div></a></div>')


def _page(title, note, body):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{PAGE_STYLE}</style></head><body>'
            f'<h1>{html.escape(title)}</h1><p class="note">{html.escape(note)}</p>{body}</body></html>')


def unique_path(path):
    """path if free, else 'name (2).ext', 'name (3).ext', ... -- an existing file is never reused."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def write_page(library, prefix, page_html):
    os.makedirs(index_dir(library), exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    path = unique_path(os.path.join(index_dir(library), f"{prefix}_{stamp}.html"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page_html)
    return path


def maybe_open(path, wanted):
    if wanted:
        import webbrowser
        webbrowser.open(pathlib.Path(os.path.abspath(path)).as_uri())


# --- COMMANDS ---

def cmd_index(args):
    return 0 if update_index(args.dir, args.batch_size) is not None else 2


def cmd_search(args):
    index = update_index(args.dir, args.batch_size)
    if index is None:
        return 2
    if not index["paths"]:
        print("❌ No maps to search.")
        return 1
    loaded = load_model()
    if loaded is None:
        return 2
    model, processor = loaded
    top = min(args.top, MAX_SEARCH_RESULTS)
    if top < args.top:
        print(f"(Showing the best {top}; a page never holds more than {MAX_SEARCH_RESULTS} maps.)")
    ranked = rank(model, processor, index, args.query)[:top]

    print(f"\n🔎 Best matches for \"{args.query}\":")
    for place, (score, position) in enumerate(ranked, 1):
        print(f"  {place:>3}. {score:.3f}  {index['paths'][position]}")

    if args.json:
        # One line the map-tool window (fgu_ui.py) reads; the thumbnail name is what it shows.
        print("FGU_RESULTS " + json.dumps([
            {"rel": index["paths"][position], "thumb": index["thumbs"][position], "score": round(score, 4)}
            for score, position in ranked]))
    if args.no_page:
        return 0
    cards = "".join(_card(args.dir, index, position, score) for score, position in ranked)
    slug = re.sub(r"[^a-z0-9]+", "-", args.query.lower()).strip("-")[:40] or "search"
    path = write_page(args.dir, f"search_{slug}",
                      _page(f"Maps matching \"{args.query}\"",
                            f"Top {len(ranked)} of {len(index['paths'])}, best first. Click a map to open it.",
                            f'<div class="grid">{cards}</div>'))
    print(f"\n🖼  Picture page: {path}")
    maybe_open(path, args.open)
    return 0


def sheet_pages(library, index, per_page=SHEET_PAGE_SIZE):
    """{file name: html} for a picture sheet of the whole library, split so that no page
    ever holds more than per_page maps: an index page ('index.html') listing each folder
    with its map count and links to its pages, and one or more pages per folder. The pages
    sit one level below the thumbnails folder, hence the '../'."""
    groups = {}
    for position, rel in enumerate(index["paths"]):
        groups.setdefault(rel.split("/")[0] if "/" in rel else "", []).append(position)
    pages = {}
    rows = ""
    for number, folder in enumerate(sorted(groups, key=lambda f: (f != "", f.lower())), 1):
        positions = groups[folder]
        label = folder or "(top level)"
        slug = re.sub(r"[^a-z0-9]+", "-", folder.lower()).strip("-")[:30] or "top-level"
        count = max(1, -(-len(positions) // per_page))
        names = [f"{number:03d}-{slug}_p{n}.html" for n in range(1, count + 1)]
        for n, name in enumerate(names):
            nav = '<p class="nav"><a href="index.html">&larr; All folders</a>'
            if n > 0:
                nav += f' &middot; <a href="{names[n - 1]}">Previous</a>'
            nav += f" &middot; Page {n + 1} of {count}"
            if n < count - 1:
                nav += f' &middot; <a href="{names[n + 1]}">Next</a>'
            nav += "</p>"
            cards = "".join(_card(library, index, p, thumbs_prefix="../thumbs/")
                            for p in positions[n * per_page:(n + 1) * per_page])
            pages[name] = _page(f"{label} ({len(positions)} maps)", "Click a map to open it.",
                                f'{nav}<div class="grid">{cards}</div>{nav}')
        links = " ".join(f'<a href="{name}">{n}</a>' for n, name in enumerate(names, 1))
        rows += f"<li><strong>{html.escape(label)}</strong> &middot; {len(positions)} maps &middot; pages {links}</li>"
    first = {"index.html": _page("Map library", f"{len(index['paths'])} maps in {len(groups)} folders, "
                                                f"at most {per_page} per page. Pick a folder.",
                                 f'<ul class="folders">{rows}</ul>')}
    first.update(pages)
    return first


def write_sheet(library, pages):
    """Writes the pages into a new folder of their own inside the index folder (never into
    an existing one) and returns the path of its index page."""
    os.makedirs(index_dir(library), exist_ok=True)
    folder = unique_path(os.path.join(index_dir(library), "sheet_" + time.strftime("%Y-%m-%d_%H%M%S")))
    os.makedirs(folder)
    for name, page_html in pages.items():
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(page_html)
    return os.path.join(folder, "index.html")


def cmd_sheet(args):
    index = update_index(args.dir, args.batch_size)
    if index is None:
        return 2
    if not index["paths"]:
        print("❌ No maps to show.")
        return 1
    pages = sheet_pages(args.dir, index, args.per_page)
    path = write_sheet(args.dir, pages)
    print(f"\n🖼  {len(index['paths'])} maps split across {len(pages) - 1} page(s), at most {args.per_page} each.")
    print(f"   Start here: {path}")
    maybe_open(path, args.open)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Find maps in a library by describing them.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (("index", "read the library and remember what each map looks like"),
                           ("search", "rank the library against a description and write a picture page"),
                           ("sheet", "write a picture page of the whole library, grouped by folder")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--dir", required=True, help="the map library folder (sub-folders are included)")
        p.add_argument("--batch-size", type=int, default=16, help="images per model pass (default: 16)")
        if name != "index":
            p.add_argument("--open", action="store_true", help="open the page in your browser when done")
        if name == "sheet":
            p.add_argument("--per-page", type=int, default=SHEET_PAGE_SIZE,
                           help=f"most maps on one page (default: {SHEET_PAGE_SIZE}, at most 500)")
        if name == "search":
            p.add_argument("query", help='what you are looking for, e.g. "tavern interior at night"')
            p.add_argument("--top", type=int, default=24,
                           help=f"how many results to show (default: 24, at most {MAX_SEARCH_RESULTS})")
            p.add_argument("--no-page", action="store_true", help="don't write the HTML picture page")
            p.add_argument("--json", action="store_true",
                           help="also print the results as one machine-readable line (used by fgu_ui.py)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.dir):
        print(f"❌ Not a folder: {args.dir}")
        return 1
    if args.batch_size < 1 or (args.command == "search" and args.top < 1):
        print("❌ --batch-size and --top must be at least 1.")
        return 1
    if args.command == "sheet" and not 1 <= args.per_page <= 500:
        print("❌ --per-page must be between 1 and 500.")
        return 1
    return {"index": cmd_index, "search": cmd_search, "sheet": cmd_sheet}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
