#!/usr/bin/env python3
"""
Safety tests for classify_maps.py and detect_grid.py.

"Safety" here means: neither script does anything destructive beyond what's
explicitly allowed --
  - classify_maps.py may MOVE map images into category subfolders (bringing
    their matching .xml sidecar along), but only once the user has opted in
    (-y, or answering "y" to the confirmation prompt), and never by
    overwriting a different file that's already at the destination.
  - detect_grid.py may create or overwrite a map's .xml sidecar, but only
    once the user has opted in (-f/--force); otherwise an existing sidecar
    must be left completely untouched.
  - Neither script writes anywhere outside the directory tree(s) it was
    actually pointed at (--dir / --move-to), wherever else on the filesystem
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

Known gap: only --dir mode is exercised. --album mode's two side effects
(the Photos-keyword AppleScript write, and osxphotos' own internal file
export) aren't covered -- there's no sandboxed Photos library to safely test
against, and intercepting a third-party library's internal writes would be
far more fragile than the interception above.

Run from inside the project's venv (same one run_classifier_map.command and
run_grid_detector.command set up), since these scripts' own dependencies
(torch, transformers, opencv-python, ...) need to be installed:

    source venv/bin/activate
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python3 test_safety.py

The very first run will be slow while classify_maps.py downloads the CLIP
model; it's cached after that. The HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE env
vars matter on every run after that first one: without them, transformers
still does an online freshness check (a HEAD request per model file) before
falling back to the cache, and if huggingface.co is slow or unreachable that
turns into minutes of retries with backoff -- each of the four classify_maps
tests pays that cost separately. With the model already cached, forcing
offline mode skips the network entirely and each test runs in a few seconds.
"""
import builtins
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
    detect_grid.py to find a real grid, and a perfectly fine throwaway image
    for classify_maps.py's file-handling tests (its content doesn't matter
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
    """Sorted paths of every file under root, relative to root."""
    found = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            found.append(os.path.relpath(os.path.join(dirpath, f), root))
    return sorted(found)


# ---------------------------------------------------------------------------
# Repo-untouched check (secondary layer -- see WriteRecorder below for the
# primary, complete-coverage check).
# ---------------------------------------------------------------------------
REPO_IGNORE_DIRNAMES = ("venv", "__pycache__", ".git")


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
# scripts depend on, not by classify_maps.py's or detect_grid.py's own logic
# (neither imports tempfile, and neither references the HF cache directly --
# grep the source if in doubt). Anything not matching one of these two is
# still checked against allowed_roots as normal.
IGNORE_WRITE_ROOTS = [
    # transformers/huggingface_hub's own CLIP model download cache -- a
    # read-only model artifact, not user data.
    os.path.realpath(os.path.expanduser("~/.cache/huggingface")),
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
        self._real_move = shutil.move
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

    def _patched_move(self, src, dst, *args, **kwargs):
        self._record(dst)
        return self._real_move(src, dst, *args, **kwargs)

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

        builtins.open = self._patched_open
        shutil.move = self._patched_move
        os.makedirs = self._patched_makedirs
        ET.ElementTree.write = patched_et_write
        return self

    def __exit__(self, *exc_info):
        builtins.open = self._real_open
        shutil.move = self._real_move
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


def run_in_process(script, script_args, allowed_roots, stdin_text=""):
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
                check(e.code in (0, None), f"{script} exited cleanly (code={e.code!r})")
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
DESTRUCTIVE_PATTERNS = [r"\bos\.remove\(", r"\bos\.unlink\(", r"\bshutil\.rmtree\(", r"\.truncate\("]


def test_no_destructive_calls_in_source():
    print("\n[static] no delete/truncate calls in either script")
    for script in ("classify_maps.py", "detect_grid.py"):
        source = open(os.path.join(REPO_ROOT, script)).read()
        for pattern in DESTRUCTIVE_PATTERNS:
            check(re.search(pattern, source) is None, f"{script} does not call {pattern}")


# ---------------------------------------------------------------------------
# classify_maps.py
# ---------------------------------------------------------------------------
def test_classify_no_move_leaves_everything_untouched():
    print("\n[classify_maps.py] --no-move touches nothing")
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "map.png")
        make_grid_image(img)
        before = read_bytes(img)

        run_in_process("classify_maps.py", ["--dir", tmp, "--no-move"], allowed_roots=[tmp])
        check(all_files(tmp) == ["map.png"], "no files created or removed")
        check(read_bytes(img) == before, "image content unchanged")


def test_classify_declines_without_confirmation():
    print("\n[classify_maps.py] no -y and empty stdin -> nothing moved")
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "map.png")
        make_grid_image(img)
        before = read_bytes(img)

        # No --move-to, no -y, no stdin to answer the prompts with: both the
        # destination prompt and the confirmation prompt hit EOF, and the
        # confirmation prompt's EOF is treated as "no".
        run_in_process("classify_maps.py", ["--dir", tmp], allowed_roots=[tmp])
        check(all_files(tmp) == ["map.png"], "no files created or removed")
        check(read_bytes(img) == before, "image content unchanged")


def test_classify_move_is_lossless_and_collision_safe():
    print("\n[classify_maps.py] -y actually moves, without corrupting or overwriting")
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "map.png")
        make_grid_image(img)
        original_bytes = read_bytes(img)

        # A pre-existing, unrelated file already sitting where the mover
        # would naturally want to place the classified file.
        collision_dir = os.path.join(tmp, "unclassified")
        os.makedirs(collision_dir)
        collision_path = os.path.join(collision_dir, "map.png")
        with open(collision_path, "wb") as f:
            f.write(b"PRE-EXISTING SENTINEL CONTENT -- MUST NOT BE OVERWRITTEN")
        sentinel_bytes = read_bytes(collision_path)

        # An impossible threshold forces a deterministic category, so we know
        # exactly which subfolder the mover will target.
        run_in_process("classify_maps.py", [
            "--dir", tmp, "--move-to", tmp, "-y",
            "--unclassified-threshold", "999",
        ], allowed_roots=[tmp])
        check(not os.path.exists(img), "original location no longer has the file (it moved)")
        check(read_bytes(collision_path) == sentinel_bytes, "pre-existing file at the natural destination is untouched")

        moved = [f for f in all_files(tmp) if f != "unclassified/map.png"]
        check(len(moved) == 1, f"exactly one new file appeared alongside the pre-existing one, found: {moved}")
        if moved:
            check(read_bytes(os.path.join(tmp, moved[0])) == original_bytes,
                  "moved file's content is byte-identical to the original (no corruption)")


def test_classify_moves_sidecar_with_image():
    print("\n[classify_maps.py] moving an image brings its .xml sidecar along, untouched")
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "map.png")
        make_grid_image(img)
        sidecar = os.path.join(tmp, "map.xml")
        with open(sidecar, "w") as f:
            f.write('<root version="4.1" dataversion="20210302"><gridsize>50,50</gridsize></root>')
        sidecar_bytes = read_bytes(sidecar)

        run_in_process("classify_maps.py", [
            "--dir", tmp, "--move-to", tmp, "-y",
            "--unclassified-threshold", "999",
        ], allowed_roots=[tmp])
        check(not os.path.exists(sidecar), "sidecar no longer at the old location")
        new_sidecar = os.path.join(tmp, "unclassified", "map.xml")
        check(os.path.exists(new_sidecar), "sidecar followed the image to its new location")
        if os.path.exists(new_sidecar):
            check(read_bytes(new_sidecar) == sidecar_bytes, "sidecar content unchanged by the move")


def test_classify_move_to_different_tree_only_touches_both_named_dirs():
    print("\n[classify_maps.py] --move-to a different directory only touches source + destination")
    with tempfile.TemporaryDirectory() as sandbox:
        source_dir = os.path.join(sandbox, "source")
        dest_dir = os.path.join(sandbox, "destination")
        # A decoy sibling directory representing something like a real Fantasy
        # Grounds campaign folder -- outside both source and destination, and
        # must survive completely untouched.
        decoy_dir = os.path.join(sandbox, "other_fantasy_grounds_data")
        os.makedirs(source_dir)
        os.makedirs(dest_dir)
        os.makedirs(decoy_dir)

        img = os.path.join(source_dir, "map.png")
        make_grid_image(img)
        decoy_file = os.path.join(decoy_dir, "campaign.xml")
        with open(decoy_file, "w") as f:
            f.write("UNRELATED FANTASY GROUNDS DATA -- MUST NOT BE TOUCHED")
        decoy_bytes = read_bytes(decoy_file)

        run_in_process("classify_maps.py", [
            "--dir", source_dir, "--move-to", dest_dir, "-y",
            "--unclassified-threshold", "999",
        ], allowed_roots=[source_dir, dest_dir])
        check(read_bytes(decoy_file) == decoy_bytes, "unrelated sibling directory completely untouched")
        check(all_files(source_dir) == [], "source directory is empty after the move")
        check(all_files(dest_dir) == ["unclassified/map.png"], "moved file landed only in the destination")


# ---------------------------------------------------------------------------
# detect_grid.py
# ---------------------------------------------------------------------------
def test_detect_grid_only_creates_the_sidecar():
    print("\n[detect_grid.py] writes only the .xml, never touches the image")
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "map.png")
        make_grid_image(img)
        before = read_bytes(img)

        run_in_process("detect_grid.py", ["--dir", tmp], allowed_roots=[tmp])
        check(read_bytes(img) == before, "image content unchanged")
        check(all_files(tmp) == ["map.png", "map.xml"], "only the sidecar was added")


def test_detect_grid_skips_existing_sidecar_without_force():
    print("\n[detect_grid.py] existing sidecar left untouched without -f")
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "map.png")
        make_grid_image(img)
        sidecar = os.path.join(tmp, "map.xml")
        sentinel = '<root version="4.1" dataversion="20210302"><gridsize>999,999</gridsize></root>'
        with open(sidecar, "w") as f:
            f.write(sentinel)

        run_in_process("detect_grid.py", ["--dir", tmp], allowed_roots=[tmp])
        check(read_bytes(sidecar).decode() == sentinel, "existing sidecar is byte-for-byte unchanged")


def test_detect_grid_force_overwrites_but_preserves_other_data():
    print("\n[detect_grid.py] -f updates the grid but preserves unrelated existing sidecar data")
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

        run_in_process("detect_grid.py", ["--dir", tmp, "-f"], allowed_roots=[tmp])
        updated = open(sidecar).read()
        check("999,999" not in updated, "stale sentinel gridsize value was actually replaced")
        check("<occluder>" in updated and "-5.0,-5.0,5.0,5.0" in updated,
              "unrelated occluder data survived the overwrite")


def main():
    test_no_destructive_calls_in_source()
    test_classify_no_move_leaves_everything_untouched()
    test_classify_declines_without_confirmation()
    test_classify_move_is_lossless_and_collision_safe()
    test_classify_moves_sidecar_with_image()
    test_classify_move_to_different_tree_only_touches_both_named_dirs()
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
