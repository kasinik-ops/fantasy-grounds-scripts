#!/usr/bin/env python3
"""
Safety tests for fgu_find_maps.py and fgu_detect_grid.py.

"Safety" here means: neither script does anything destructive beyond what's
explicitly allowed --
  - fgu_find_maps.py only reads the maps; everything it writes (the index, the
    thumbnails and the picture pages) goes inside the library's .fgu_map_index
    folder, and a picture page is never overwritten.
  - fgu_detect_grid.py may create or overwrite a map's .xml sidecar, but only
    once the user has opted in (-f/--force); otherwise an existing sidecar
    must be left completely untouched.
  - Neither script writes anywhere outside the directory tree it was
    actually pointed at (--dir), wherever else on the filesystem
    that might be -- including, and especially, real Fantasy Grounds data.

How that last, broadest property is checked: rather than snapshotting a list
of plausible "danger zone" locations and hoping that covers it, every
write-capable call these scripts use (shutil.move, os.makedirs,
ET.ElementTree.write, and open() in a writing mode -- see WriteRecorder) is
intercepted for the duration of the run, and every path it targeted is
checked against the directories the test actually told the script to use.
That's a complete account of every write the script made, not a sample, so
it holds regardless of where on disk something might have gone wrong.

Each script runs in-process via runpy (same argv, same __main__ entry point
as `python script.py <args>` would use from the command line) rather than as
a subprocess, specifically so the interception above can see every write --
patches to builtins.open etc. don't cross a subprocess boundary. Nothing
about the scripts' own logic is touched or mocked; only the OS-level
primitives they call are wrapped to record what they were asked to do before
letting the real call through.

As a second, independent layer, every run also snapshots the whole repo
(outside venv/__pycache__/.git) before and after and fails if a single file
in it was added, removed, or modified -- these scripts have no legitimate
reason to write into their own project directory, ever.

Run from inside the project's venv (same one the launchers set up), since these
scripts' own dependencies (torch, transformers, opencv-python, ...) need to be
installed. On macOS:

    source venv/bin/activate
    python3 fgu_support_test_safety.py

On Windows, run both launchers' setup once, then use the venv they created
(it needs both tools' packages, because this test imports numpy and cv2 itself):

    powershell -ExecutionPolicy Bypass -File run_windows.ps1 -Tool find -SetupOnly
    powershell -ExecutionPolicy Bypass -File run_windows.ps1 -Tool grid -SetupOnly
    venv\\Scripts\\python.exe fgu_support_test_safety.py

CI (.github/workflows/ci.yml) runs it exactly this way on both platforms.

The tests need the CLIP model in the local cache (they never go online): run
`python fgu_find_maps.py download` once first, or press "Update AI model cache"
in the window.
"""
import builtins
import getpass
import io
import os
import re
import runpy
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

import numpy as np
import cv2

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

failures = []


def check(condition, message):
    if condition:
        print(f"  ok   - {message}")
    else:
        print(f"  FAIL - {message}")
        failures.append(message)


def make_grid_image(path, spacing=50, size=300):
    """A synthetic image with a visible square grid drawn on it: enough for
    fgu_detect_grid.py to find a real grid, and a perfectly fine throwaway image
    for fgu_find_maps.py's file-handling tests (its content doesn't matter
    there, only that it's a valid, byte-comparable image file)."""
    rng = np.random.default_rng(0)
    img = rng.integers(80, 120, size=(size, size, 3)).astype(np.uint8)
    for x in range(0, size, spacing):
        img[:, x:x + 2] = (30, 30, 30)
    for y in range(0, size, spacing):
        img[y:y + 2, :] = (30, 30, 30)
    cv2.imwrite(path, img)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def all_files(root):
    """Sorted paths of every file under root, relative to root, always with "/"
    separators so the expected values in the tests below read the same on
    macOS and Windows."""
    found = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            found.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
    return sorted(found)


# ---------------------------------------------------------------------------
# Repo-untouched check (secondary layer -- see WriteRecorder below for the
# primary, complete-coverage check).
# ---------------------------------------------------------------------------
# windows_tools is the Windows launcher's private Python/uv folder: thousands of
# files, gitignored, and (like venv) not something these scripts ever write to.
REPO_IGNORE_DIRNAMES = ("venv", "windows_tools", "__pycache__", ".git")


def snapshot_repo():
    """{relative_path: (size, mtime_ns)} for every file in REPO_ROOT, outside
    REPO_IGNORE_DIRNAMES."""
    snap = {}
    for dirpath, dirnames, files in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in REPO_IGNORE_DIRNAMES]
        for f in files:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, REPO_ROOT)
            st = os.stat(full)
            snap[rel] = (st.st_size, st.st_mtime_ns)
    return snap


def assert_repo_untouched(before, after):
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in (set(before) & set(after)) if before[p] != after[p])
    check(not added, f"no new files appeared in the repo itself, found: {added}")
    check(not removed, f"no repo files were deleted, found: {removed}")
    check(not changed, f"no repo files were modified, found: {changed}")


# Narrow, explicit, and justified: writes made by third-party libraries these
# scripts depend on, not by fgu_find_maps.py's or fgu_detect_grid.py's own logic
# (neither imports tempfile, and neither references the HF cache directly --
# grep the source if in doubt). Anything not matching one of these is still
# checked against allowed_roots as normal.
IGNORE_WRITE_ROOTS = [
    # transformers/huggingface_hub's own CLIP model download cache -- a
    # read-only model artifact, not user data.
    os.path.realpath(os.path.expanduser("~/.cache/huggingface")),
    # PyTorch's own compile cache, <system temp>/torchinductor_<user>. Newer
    # torch creates it just by being imported, which is why it never showed up
    # on an older local install and first appeared in CI (macOS and Windows
    # alike). An exact path rather than a pattern, so it can't hide a real
    # stray write elsewhere in the temp folder.
    os.path.realpath(os.path.join(tempfile.gettempdir(), "torchinductor_" + getpass.getuser())),
]
IGNORE_WRITE_BASENAMES = {
    # A one-time JIT scaffold file torch.distributed writes into its own
    # fresh tempfile.mkdtemp() directory the first time a certain torch
    # submodule is touched during model loading -- a known torch quirk.
    "_remote_module_non_scriptable.py",
}


# ---------------------------------------------------------------------------
# WriteRecorder: the primary check. Intercepts every write-capable call these
# scripts use and records the real, resolved path each one targeted.
# ---------------------------------------------------------------------------
class WriteRecorder:
    WRITE_MODE_CHARS = set("wax+")  # any of these in an open() mode means "can write"

    def __init__(self):
        self.touched = []
        self._real_open = builtins.open
        self._real_io_open = io.open
        self._real_move = shutil.move
        self._real_replace = os.replace
        self._real_rename = os.rename
        self._real_makedirs = os.makedirs
        self._real_et_write = ET.ElementTree.write

    def _record(self, path):
        try:
            self.touched.append(os.path.realpath(os.fspath(path)))
        except TypeError:
            pass  # not a path-like target (e.g. an in-memory buffer) -- nothing to check

    def _patched_open(self, file, mode="r", *args, **kwargs):
        if self.WRITE_MODE_CHARS & set(mode):
            self._record(file)
        return self._real_open(file, mode, *args, **kwargs)

    # Moves, renames and replaces change BOTH ends: the destination gains a file
    # and the source loses one, so both must be somewhere the run was told to use.
    def _patched_move(self, src, dst, *args, **kwargs):
        self._record(src)
        self._record(dst)
        return self._real_move(src, dst, *args, **kwargs)

    def _patched_replace(self, src, dst, *args, **kwargs):
        self._record(src)
        self._record(dst)
        return self._real_replace(src, dst, *args, **kwargs)

    def _patched_rename(self, src, dst, *args, **kwargs):
        self._record(src)
        self._record(dst)
        return self._real_rename(src, dst, *args, **kwargs)

    def _patched_makedirs(self, name, *args, **kwargs):
        self._record(name)
        return self._real_makedirs(name, *args, **kwargs)

    def __enter__(self):
        recorder, real_et_write = self, self._real_et_write

        # ET.ElementTree.write is called as tree_instance.write(path, ...), which
        # goes through the descriptor protocol on the class attribute. A bound
        # method assigned there does NOT get re-bound to tree_instance (only plain
        # functions do), so it must be a plain function closing over `recorder`,
        # not recorder._patched_et_write itself -- using the bound method here
        # silently drops tree_instance and shifts every argument over by one.
        def patched_et_write(tree_self, file_or_filename, *args, **kwargs):
            recorder._record(file_or_filename)
            return real_et_write(tree_self, file_or_filename, *args, **kwargs)

        # io.open is the very same function as builtins.open, but patching one name
        # doesn't change the other -- and np.savez, zipfile and pathlib all call
        # io.open, so patching only builtins.open would leave them invisible.
        builtins.open = self._patched_open
        io.open = self._patched_open
        shutil.move = self._patched_move
        os.replace = self._patched_replace
        os.rename = self._patched_rename
        os.makedirs = self._patched_makedirs
        ET.ElementTree.write = patched_et_write
        return self

    def __exit__(self, *exc_info):
        builtins.open = self._real_open
        io.open = self._real_io_open
        shutil.move = self._real_move
        os.replace = self._real_replace
        os.rename = self._real_rename
        os.makedirs = self._real_makedirs
        ET.ElementTree.write = self._real_et_write

    def outside(self, allowed_roots):
        roots = [os.path.realpath(r) for r in allowed_roots]

        def is_allowed(path):
            if any(path == r or path.startswith(r + os.sep) for r in roots):
                return True
            if any(path == p or path.startswith(p + os.sep) for p in IGNORE_WRITE_ROOTS):
                return True
            if os.path.basename(path) in IGNORE_WRITE_BASENAMES:
                return True
            return False

        return [p for p in self.touched if not is_allowed(p)]


def run_in_process(script, script_args, allowed_roots, stdin_text="", expect_exit=(0, None)):
    """Runs one of the real scripts in-process -- same argv, same __main__
    entry point as `python script.py <args>` from the command line -- with
    every write-capable call intercepted. Asserts every write stayed inside
    allowed_roots and that the repo itself was untouched; callers only need
    to check their own fixture-specific outcomes afterwards."""
    script_path = os.path.join(REPO_ROOT, script)
    old_argv, old_stdin = sys.argv, sys.stdin
    sys.argv = [script] + script_args
    sys.stdin = io.StringIO(stdin_text)
    repo_before = snapshot_repo()
    recorder = WriteRecorder()
    try:
        with recorder:
            try:
                runpy.run_path(script_path, run_name="__main__")
            except SystemExit as e:
                check(e.code in expect_exit, f"{script} exited as expected (code={e.code!r})")
    except Exception as e:
        check(False, f"{script} raised an unexpected exception: {e!r}")
    finally:
        sys.argv, sys.stdin = old_argv, old_stdin

    outside = recorder.outside(allowed_roots)
    check(not outside, f"every write from {script} stayed inside {allowed_roots}, found writes outside: {outside}")
    assert_repo_untouched(repo_before, snapshot_repo())
    return recorder


# ---------------------------------------------------------------------------
# Static check: neither script should contain a delete/truncate call at all.
# ---------------------------------------------------------------------------
DESTRUCTIVE_PATTERNS = [
    r"\bos\.remove\(", r"\.unlink\(", r"\bshutil\.rmtree\(", r"\.truncate\(",
    r"\.rmdir\(", r"\bos\.removedirs\(",
]


def test_no_destructive_calls_in_source():
    print("\n[static] no delete/truncate calls in any script")
    for script in ("fgu_detect_grid.py", "fgu_find_maps.py", "fgu_ui.py"):
        source = open(os.path.join(REPO_ROOT, script)).read()
        for pattern in DESTRUCTIVE_PATTERNS:
            check(re.search(pattern, source) is None, f"{script} does not call {pattern}")


def test_find_maps_ignores_unsafe_index_entries():
    print("\n[fgu_find_maps.py] an index that came with a download can't point outside the library")
    import importlib
    sys.path.insert(0, REPO_ROOT)
    sys.argv = ["fgu_find_maps.py"]
    sys.modules.pop("fgu_find_maps", None)
    m = importlib.import_module("fgu_find_maps")
    good_thumb = "0123456789abcdef0123.jpg"
    z = np.zeros(8, np.float32)
    bad_paths = ["../outside.png", "sub/../../outside.png", "/etc/passwd.png", "C:/Windows/x.png", ".hidden/x.png", "payload.bat",
                 "run.exe", "sub//x.png", "", "x.png\x00.png"]
    with tempfile.TemporaryDirectory() as tmp:
        entries = [("ok/a.png", 1, 1, good_thumb, z), ("b.JPG", 1, 1, good_thumb, z)]
        entries += [(p, 1, 1, good_thumb, z) for p in bad_paths]
        entries += [("evil_thumb.png", 1, 1, "../../evil.jpg", z), ("short_thumb.png", 1, 1, "abc.jpg", z)]
        m.save_index(tmp, entries)
        loaded = m.load_index(tmp)
        check(loaded["paths"] == ["b.JPG", "ok/a.png"] or sorted(loaded["paths"]) == ["b.JPG", "ok/a.png"],
              f"only the two safe entries are loaded (got {loaded['paths']})")
        check(len(loaded["embeddings"]) == len(loaded["paths"]) == len(loaded["sizes"]) == len(loaded["thumbs"]),
              "...and every column still lines up")
    check(m.is_safe_rel("a/b.png") and m.is_safe_rel("Maps/Cave 2.JPEG") and m.is_safe_rel("Dungeon: Level 2.png"),
          "ordinary names, including spaces, capitals and a colon later in the name, are accepted")
    check(not m.is_safe_rel("C:evil.png"), "a name that starts like a Windows drive is not")


# ---------------------------------------------------------------------------
# fgu_find_maps.py
# ---------------------------------------------------------------------------
CACHE_DIR = ".fgu_map_index"


def _make_find_library(tmp):
    """tmp/lib with one map at the top and one in a sub-folder. Returns (lib, {relative path: bytes})."""
    lib = os.path.join(tmp, "lib")
    os.makedirs(os.path.join(lib, "caves"))
    make_grid_image(os.path.join(lib, "top.png"), spacing=40)
    make_grid_image(os.path.join(lib, "caves", "deep.png"), spacing=60)
    return lib, {rel: read_bytes(os.path.join(lib, *rel.split("/"))) for rel in all_files(lib)}


def _outside_cache(lib):
    return [f for f in all_files(lib) if not f.startswith(CACHE_DIR + "/")]


def test_find_maps_writes_only_its_cache_and_never_touches_maps():
    print("\n[fgu_find_maps.py] index/search/sheet write only inside .fgu_map_index and only read the maps")
    with tempfile.TemporaryDirectory() as tmp:
        lib, originals = _make_find_library(tmp)

        run_in_process("fgu_find_maps.py", ["index", "--dir", lib], allowed_roots=[lib])
        check(sorted(_outside_cache(lib)) == sorted(originals), "after index, no file outside the cache folder was added or removed")
        check(all(read_bytes(os.path.join(lib, *rel.split("/"))) == b for rel, b in originals.items()),
              "after index, every map is byte-for-byte unchanged")
        cache_files = [f for f in all_files(lib) if f.startswith(CACHE_DIR + "/")]
        check(CACHE_DIR + "/index.npz" in cache_files, "the index was written")
        check(len([f for f in cache_files if f.startswith(CACHE_DIR + "/thumbs/")]) == 2, "one thumbnail per map was written")
        check(not any(f.endswith(".tmp") for f in cache_files), "no half-written temporary file was left behind")

        run_in_process("fgu_find_maps.py", ["search", "--dir", lib, "a stone dungeon corridor", "--top", "5"], allowed_roots=[lib])
        run_in_process("fgu_find_maps.py", ["sheet", "--dir", lib], allowed_roots=[lib])
        check(sorted(_outside_cache(lib)) == sorted(originals), "after search and sheet, still nothing outside the cache folder")
        check(all(read_bytes(os.path.join(lib, *rel.split("/"))) == b for rel, b in originals.items()),
              "after search and sheet, every map is still byte-for-byte unchanged")
        pages = [f for f in all_files(lib) if f.endswith(".html")]
        check(len(pages) >= 2 and all(f.startswith(CACHE_DIR + "/") for f in pages),
              f"the picture pages were written inside the cache folder, found {pages}")


def test_find_maps_picks_up_changes_and_never_overwrites_a_page():
    print("\n[fgu_find_maps.py] a changed library re-indexes only what changed; a page is never overwritten")
    import importlib, time
    sys.path.insert(0, REPO_ROOT)
    sys.argv = ["fgu_find_maps.py"]
    sys.modules.pop("fgu_find_maps", None)
    m = importlib.import_module("fgu_find_maps")
    with tempfile.TemporaryDirectory() as tmp:
        lib, originals = _make_find_library(tmp)
        idx = os.path.join(lib, CACHE_DIR, "index.npz")

        run_in_process("fgu_find_maps.py", ["index", "--dir", lib], allowed_roots=[lib])
        before = m.load_index(lib)
        check(before is not None and sorted(before["paths"]) == ["caves/deep.png", "top.png"], "the index lists both maps, sub-folder included")

        # A map moved to another folder (for example by hand) keeps its embedding: no model needed.
        os.makedirs(os.path.join(lib, "towns"))
        shutil.move(os.path.join(lib, "top.png"), os.path.join(lib, "towns", "top.png"))
        recorder = WriteRecorder()
        with recorder:
            after = m.update_index(lib)
        check(not recorder.outside([lib]), "re-indexing wrote only inside the library")
        check(after is not None and sorted(after["paths"]) == ["caves/deep.png", "towns/top.png"], "the moved map is found at its new place")
        old_row = before["embeddings"][before["paths"].index("top.png")]
        new_row = after["embeddings"][after["paths"].index("towns/top.png")]
        check(np.allclose(old_row, new_row), "the moved map kept its embedding (it wasn't read again)")

        # A map that can't be read is skipped and remembered, not retried on every run.
        with open(os.path.join(lib, "broken.png"), "wb") as f:
            f.write(b"not an image")
        m.update_index(lib)
        again = m.load_index(lib)["failed"]
        check(len(again) == 1 and list(again)[0][0] == "broken.png", "the unreadable file is remembered as unreadable")

        # Page names: same second, same prefix -> the second page gets the next free name.
        real_strftime = time.strftime
        time.strftime = lambda fmt, *a: "FIXED"
        try:
            with WriteRecorder() as rec:
                first = m.write_page(lib, "page", "<p>first</p>")
                second = m.write_page(lib, "page", "<p>second</p>")
        finally:
            time.strftime = real_strftime
        check(not rec.outside([lib]), "writing pages touched only the library")
        check(os.path.basename(first) == "page_FIXED.html" and os.path.basename(second) == "page_FIXED (2).html",
              f"the second page took the next free name, got {os.path.basename(first)!r} and {os.path.basename(second)!r}")
        check(read_bytes(first) == b"<p>first</p>", "the first page is byte-for-byte as it was written")


def test_find_maps_sheet_is_split_into_small_pages():
    print("\n[fgu_find_maps.py] sheet never puts the whole library on one page")
    import importlib
    import re as regex
    sys.path.insert(0, REPO_ROOT)
    sys.argv = ["fgu_find_maps.py"]
    sys.modules.pop("fgu_find_maps", None)
    m = importlib.import_module("fgu_find_maps")

    # 1. Pure: a 4,322-map index (no model, no files), the size of the real library.
    paths = ([f"dungeons/m{i:05d}.png" for i in range(1202)] + [f"unclassified/m{i:05d}.png" for i in range(1192)]
             + [f"folder{n:02d}/m{i:03d}.png" for n in range(60) for i in range(30)] + [f"top{i}.png" for i in range(128)])
    fake = {"paths": paths, "thumbs": [f"{i:020x}.jpg" for i in range(len(paths))]}
    pages = m.sheet_pages("/library", fake)
    cards = {name: html.count('class="card"') for name, html in pages.items()}
    check(len(paths) == 4322, "the fake library has 4,322 maps")
    check(max(cards.values()) <= m.SHEET_PAGE_SIZE == 96, f"no page holds more than {m.SHEET_PAGE_SIZE} maps (largest: {max(cards.values())})")
    check(cards["index.html"] == 0, "the index page lists folders, not maps")
    check(sum(cards.values()) == len(paths), "every map is on exactly one page")
    shown = regex.findall(r'src="\.\./thumbs/([0-9a-f]{20}\.jpg)"', "".join(pages.values()))
    check(len(shown) == len(set(shown)) == len(paths), "every thumbnail appears exactly once")
    index_html = pages["index.html"]
    links = regex.findall(r'href="([^"]+\.html)"', index_html)
    check(links and all(link in pages for link in links), "every link on the index page points at a page that exists")
    check(len(index_html) < 100_000, f"the index page is small ({len(index_html):,} characters)")
    check(len([n for n in pages if "dungeons" in n]) == 13, "the 1,202-map folder is split into 13 pages")
    small = m.sheet_pages("/library", fake, per_page=10)
    check(max(html.count('class="card"') for html in small.values()) <= 10, "--per-page is honoured")

    # 2. Real run: files on disk, links that resolve, nothing outside the cache folder.
    with tempfile.TemporaryDirectory() as tmp:
        lib = os.path.join(tmp, "lib")
        for folder, count in (("a", 25), ("b", 3), ("", 2)):
            os.makedirs(os.path.join(lib, folder), exist_ok=True)
            for i in range(count):
                make_grid_image(os.path.join(lib, folder, f"map{i:02d}.png"), spacing=30 + i)
        originals = {rel: read_bytes(os.path.join(lib, *rel.split("/"))) for rel in all_files(lib)}

        run_in_process("fgu_find_maps.py", ["sheet", "--dir", lib, "--per-page", "10"], allowed_roots=[lib])
        sheets = [f for f in all_files(lib) if f.startswith(CACHE_DIR + "/sheet_") and f.endswith(".html")]
        names = sorted(f.rsplit("/", 1)[-1] for f in sheets)
        check("index.html" in names and len(names) == 1 + 3 + 1 + 1,
              f"index page + 3 pages for 25 maps + 1 + 1 (got {names})")
        sheet_dir = os.path.dirname(os.path.join(lib, sheets[0]))
        broken = []
        for f in sheets:
            html = read_bytes(os.path.join(lib, f)).decode("utf-8")
            check(html.count('class="card"') <= 10, f"{f.rsplit('/', 1)[-1]} holds at most 10 maps")
            for target in regex.findall(r'(?:src|href)="(\.\./thumbs/[^"]+|[^":]+\.html)"', html):
                if not os.path.isfile(os.path.normpath(os.path.join(sheet_dir, target))):
                    broken.append((f, target))
        check(not broken, f"every thumbnail and page link resolves on disk (broken: {broken})")
        check(all(read_bytes(os.path.join(lib, rel)) == b for rel, b in originals.items()), "every map is unchanged")

        first = {f: read_bytes(os.path.join(lib, f)) for f in sheets}
        run_in_process("fgu_find_maps.py", ["sheet", "--dir", lib, "--per-page", "10"], allowed_roots=[lib])
        check(all(read_bytes(os.path.join(lib, f)) == b for f, b in first.items()), "a second run left the first run's pages untouched")
        check(m.main(["sheet", "--dir", lib, "--per-page", "0"]) == 1 and m.main(["sheet", "--dir", lib, "--per-page", "501"]) == 1,
              "--per-page outside 1-500 is refused")


# ---------------------------------------------------------------------------
# fgu_detect_grid.py
# ---------------------------------------------------------------------------
def test_detect_grid_only_creates_the_sidecar():
    print("\n[fgu_detect_grid.py] writes only the .xml, never touches the image")
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "map.png")
        make_grid_image(img)
        before = read_bytes(img)

        run_in_process("fgu_detect_grid.py", ["--dir", tmp], allowed_roots=[tmp])
        check(read_bytes(img) == before, "image content unchanged")
        check(all_files(tmp) == ["map.png", "map.xml"], "only the sidecar was added")


def test_detect_grid_skips_existing_sidecar_without_force():
    print("\n[fgu_detect_grid.py] existing sidecar left untouched without -f")
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "map.png")
        make_grid_image(img)
        sidecar = os.path.join(tmp, "map.xml")
        sentinel = '<root version="4.1" dataversion="20210302"><gridsize>999,999</gridsize></root>'
        with open(sidecar, "w") as f:
            f.write(sentinel)

        run_in_process("fgu_detect_grid.py", ["--dir", tmp], allowed_roots=[tmp])
        check(read_bytes(sidecar).decode() == sentinel, "existing sidecar is byte-for-byte unchanged")


def test_detect_grid_force_overwrites_but_preserves_other_data():
    print("\n[fgu_detect_grid.py] -f updates the grid but preserves unrelated existing sidecar data")
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "map.png")
        make_grid_image(img, spacing=50)
        sidecar = os.path.join(tmp, "map.xml")
        with open(sidecar, "w") as f:
            f.write(
                '<root version="4.1" dataversion="20210302">'
                '<gridsize>999,999</gridsize>'
                '<occluder><id>1</id><points>-5.0,-5.0,5.0,5.0</points></occluder>'
                '</root>'
            )

        run_in_process("fgu_detect_grid.py", ["--dir", tmp, "-f"], allowed_roots=[tmp])
        updated = open(sidecar).read()
        check("999,999" not in updated, "stale sentinel gridsize value was actually replaced")
        check("<occluder>" in updated and "-5.0,-5.0,5.0,5.0" in updated,
              "unrelated occluder data survived the overwrite")


def main():
    test_no_destructive_calls_in_source()
    test_find_maps_writes_only_its_cache_and_never_touches_maps()
    test_find_maps_picks_up_changes_and_never_overwrites_a_page()
    test_find_maps_ignores_unsafe_index_entries()
    test_find_maps_sheet_is_split_into_small_pages()
    test_detect_grid_only_creates_the_sidecar()
    test_detect_grid_skips_existing_sidecar_without_force()
    test_detect_grid_force_overwrites_but_preserves_other_data()

    print()
    if failures:
        print(f"❌ {len(failures)} safety check(s) failed:")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    else:
        print("✅ All safety checks passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
