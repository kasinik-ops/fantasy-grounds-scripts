#!/usr/bin/env python3
"""
Correctness tests for the model code in fgu_find_maps.py (the safety tests in
fgu_support_test_safety.py cover what it does to files; these cover whether it
still gives the right answers).

It encodes the search text once and pushes images through the model in batches,
instead of re-running the whole model for every picture. That is only acceptable
if it gives the SAME answers, so this checks, against the original
one-image-at-a-time model(**inputs) forward pass:

  - identical probabilities and identical winning description, image by image
  - the same results whatever the batch size
  - a file that can't be read costs only itself, not the rest of its batch
  - a search for a colour ranks the image of that colour first
  - print-size maps (bigger than Pillow's default decompression-bomb limit) are
    read, a big JPEG decoded at reduced size means the same thing to the model as a
    full decode, an absurdly large image is still refused, and an unreadable file is
    retried once the size limit changes
  - the fast image processor (needs torchvision) is used when it works and the standard
    one when it doesn't, gives the same embeddings, and a huge map is shrunk before it
    reaches the processor so it can't use gigabytes of memory
  - indexing never looks hung: it says it is loading the model, prints percent, speed
    and time left after every batch, announces very large maps, and speaks up if a
    map takes long

It uses the real CLIP model from the local cache (it never goes online), so run it
the same way as the safety tests -- inside the project's venv, after the model has
been downloaded (`python fgu_find_maps.py download`, or the window's "Update AI
model cache" button):

    source venv/bin/activate
    python3 fgu_support_test_model.py

CI runs it on macOS and Windows, with whatever transformers/torch versions those
resolve, which is the point: the encoding goes through CLIPModel's submodules
because the get_*_features() helpers have changed between transformers releases.
"""
import contextlib
import importlib
import io
import os
import sys
import tempfile

import numpy as np
import torch
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)
failures = []


def check(condition, message):
    if condition:
        print(f"  ok   - {message}")
    else:
        print(f"  FAIL - {message}")
        failures.append(message)


def synthetic_images():
    """A varied set: noise, gradients, flat colour, a grid-and-rooms 'map', odd sizes."""
    rng = np.random.default_rng(3)
    images = {}
    images["noise"] = rng.integers(0, 255, size=(300, 400, 3), dtype=np.uint8)
    ramp = np.linspace(0, 255, 500, dtype=np.uint8)
    images["gradient"] = np.stack([np.tile(ramp, (200, 1))] * 3, axis=-1)
    images["flat_green"] = np.full((256, 256, 3), (40, 120, 60), dtype=np.uint8)
    grid = np.full((600, 600, 3), (196, 180, 140), dtype=np.uint8)
    for x in range(0, 600, 50):
        grid[:, x:x + 2] = (60, 50, 40)
    for y in range(0, 600, 50):
        grid[y:y + 2, :] = (60, 50, 40)
    grid[100:250, 100:350] = (110, 100, 95)          # a 'room'
    images["rooms"] = grid
    images["tall"] = rng.integers(0, 255, size=(900, 150, 3), dtype=np.uint8)
    return images


def load_module():
    """Import fgu_find_maps without it parsing THIS script's command line."""
    sys.argv = ["fgu_find_maps.py"]
    sys.modules.pop("fgu_find_maps", None)
    return importlib.import_module("fgu_find_maps")


def pixels_of(m, tmp, path, processor):
    """The model-ready pixels for one picture, prepared the way indexing prepares them."""
    folder, name = os.path.split(path)
    return m.read_image(folder, name, os.path.splitext(name)[0] + ".jpg", processor)


def test_new_path_matches_the_original_forward_pass(m, model, processor, tmp):
    print("\n[model] cached-text batched path == the original per-image forward pass")
    prompts = ["a tavern interior", "a dungeon corridor", "a forest clearing", "a grid of rooms"]
    text_features = m.encode_text_features(model, processor, prompts)

    paths = {}
    for name, array in synthetic_images().items():
        paths[name] = os.path.join(tmp, f"{name}.png")
        Image.fromarray(array).save(paths[name])

    old = {}
    for name, path in paths.items():
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        inputs = processor(text=prompts, images=image, return_tensors="pt", padding=True).to(m.DEVICE)
        with torch.no_grad():
            old[name] = model(**inputs).logits_per_image.softmax(dim=1)[0].cpu()

    def probs(image_features):
        return (model.logit_scale.exp() * image_features @ text_features.T).softmax(dim=1).detach().cpu()

    pixels = torch.cat([pixels_of(m, tmp, p, processor) for p in paths.values()])
    new = probs(m.encode_image_features(model, pixels))

    for i, name in enumerate(paths):
        diff = (new[i] - old[name]).abs().max().item()
        check(diff < 1e-3, f"{name}: probabilities match (largest difference {diff:.2e})")
        check(int(new[i].argmax()) == int(old[name].argmax()), f"{name}: same winning description")

    one_at_a_time = torch.cat([probs(m.encode_image_features(model, pixels_of(m, tmp, p, processor))) for p in paths.values()])
    check((one_at_a_time - new).abs().max().item() < 1e-4, "batching doesn't change any result (batch of 5 vs one at a time)")


def test_unreadable_file_costs_only_itself(m, tmp):
    print("\n[run] an unreadable image doesn't stop the rest of its batch")
    folder = os.path.join(tmp, "mixed")
    os.makedirs(folder)
    for name, array in list(synthetic_images().items())[:3]:
        Image.fromarray(array).save(os.path.join(folder, f"{name}.png"))
    with open(os.path.join(folder, "broken.png"), "wb") as f:
        f.write(b"this is not a png")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        index = m.update_index(folder, batch_size=8)
    check(index is not None and len(index["paths"]) == 3, f"the 3 good images were all indexed (got {len(index['paths']) if index else None})")
    check(len(m.load_index(folder)["failed"]) == 1, "...and the broken one was remembered as unreadable")


def test_find_maps_ranks_sensibly(m, model, processor, tmp):
    print("\n[fgu_find_maps.py] a colour search finds that colour")
    f = m

    lib = os.path.join(tmp, "colours")
    os.makedirs(lib)
    colours = {"red": (220, 30, 30), "green": (30, 170, 60), "blue": (30, 60, 220)}
    for name, rgb in colours.items():
        Image.fromarray(np.full((300, 400, 3), rgb, dtype=np.uint8)).save(os.path.join(lib, f"{name}.png"))
    with contextlib.redirect_stdout(io.StringIO()):
        index = f.update_index(lib)
    for name in colours:
        ranked = f.rank(model, processor, index, f"a solid {name} image")
        top = index["paths"][ranked[0][1]]
        check(top == f"{name}.png", f"searching 'a solid {name} image' puts {name}.png first (got {top})")


def test_huge_maps(m, model, processor, tmp):
    print("\n[huge maps] print-size maps are read; absurd ones are still refused")
    f = m
    lib = os.path.join(tmp, "huge")
    os.makedirs(lib)

    # 195 million pixels: over Pillow's default limit (179 million), under ours.
    big = os.path.join(lib, "big.png")
    Image.new("RGB", (15000, 13000), (120, 100, 80)).save(big)
    check(15000 * 13000 > 2 * 89_478_485, "the test map really is over Pillow's default limit")
    try:
        tensor = f.read_image(lib, "big.png", "big.jpg", processor)
        check(tuple(tensor.shape) == (1, 3, 224, 224) and os.path.isfile(os.path.join(lib, ".fgu_map_index", "thumbs", "big.jpg")),
              "fgu_find_maps reads it and makes its thumbnail")
    except Exception as e:
        check(False, f"fgu_find_maps reads a map over the old limit (raised {e!r})")
    os.remove(big)

    # Reduced-size JPEG decoding must mean the same thing to the model as a full decode.
    ramp = np.linspace(0, 255, 9000, dtype=np.uint8)
    jpeg = os.path.join(lib, "big.jpg")
    Image.fromarray(np.stack([np.tile(ramp, (8000, 1)), np.tile(ramp[::-1], (8000, 1)), np.full((8000, 9000), 90, np.uint8)], axis=-1)).save(jpeg, quality=60)
    check(9000 * 8000 > f.HUGE_IMAGE_PIXELS, "the test JPEG is big enough to be decoded at reduced size")
    drafted = f.encode_image_features(model, f.read_image(lib, "big.jpg", "big_drafted.jpg", processor))
    saved, f.HUGE_IMAGE_PIXELS = f.HUGE_IMAGE_PIXELS, 10 ** 12
    try:
        full = f.encode_image_features(model, f.read_image(lib, "big.jpg", "big_full.jpg", processor))
    finally:
        f.HUGE_IMAGE_PIXELS = saved
    similarity = float((drafted * full).sum())
    check(similarity > 0.98, f"reduced-size decoding gives essentially the same embedding (cosine {similarity:.4f})")
    os.remove(jpeg)

    # Still refused: 750 million pixels, mode '1' so building it costs only ~94 MB.
    absurd = os.path.join(lib, "absurd.png")
    Image.new("1", (30000, 25000)).save(absurd)
    try:
        f.read_image(lib, "absurd.png", "absurd.jpg", processor)
        check(False, "an image over the ceiling is refused")
    except Image.DecompressionBombError:
        check(True, "an image over the ceiling is refused")
    except Exception as e:
        check(False, f"an image over the ceiling is refused (raised {e!r} instead)")
    os.remove(absurd)

    # A file that failed because of the size limit is retried when the limit changes.
    small_lib = os.path.join(tmp, "retry")
    os.makedirs(small_lib)
    Image.fromarray(synthetic_images()["rooms"]).resize((2000, 1500)).save(os.path.join(small_lib, "three_mp.png"))
    real_limit = f.Image.MAX_IMAGE_PIXELS
    try:
        f.Image.MAX_IMAGE_PIXELS = 1_000_000          # refuses anything over 2 million pixels
        with contextlib.redirect_stdout(io.StringIO()):
            index = f.update_index(small_lib)
        check(index is not None and index["paths"] == [], "with a small limit the 3-megapixel map is skipped")
        check(len(f.load_index(small_lib)["failed"]) == 1, "...and remembered as unreadable")
        f.Image.MAX_IMAGE_PIXELS = real_limit
        check(f.load_index(small_lib)["failed"] == set(), "once the limit changes it is no longer remembered as unreadable")
        with contextlib.redirect_stdout(io.StringIO()):
            index = f.update_index(small_lib)
        check(index["paths"] == ["three_mp.png"], "...and the next run reads it")
    finally:
        f.Image.MAX_IMAGE_PIXELS = real_limit


def test_fast_image_processor(m, model, processor, tmp):
    print("\n[fast processor] used when it works, standard one otherwise, same embeddings")
    import importlib.util
    import transformers
    f = m
    have_torchvision = importlib.util.find_spec("torchvision") is not None
    def is_fast(proc):
        """Does this processor use torchvision? 4.x names it ...Fast; 5.x makes it the
        CLIPImageProcessor class itself, built on TorchvisionBackend."""
        cls = type(proc.image_processor)
        return cls.__name__.endswith("Fast") or any(base.__name__ == "TorchvisionBackend" for base in cls.__mro__)

    name = type(processor.image_processor).__name__
    print(f"  info - transformers {transformers.__version__}, torchvision installed: {have_torchvision}; the finder is using {name}")
    check(is_fast(processor) == have_torchvision, "the fast image processor is used exactly when torchvision is installed")
    check(is_fast(f.load_model()[1]) == is_fast(processor), "load_model picks the same kind of processor")

    fast_kwargs = f.fast_processor_kwargs()

    class Stub:
        calls = []
        failure = ImportError("no torchvision")

        @classmethod
        def from_pretrained(cls, model_id, **kwargs):
            kwargs = {k: v for k, v in kwargs.items() if k != "local_files_only"}
            cls.calls.append(kwargs)
            if kwargs == fast_kwargs:
                raise cls.failure
            return "standard processor"

    real_transformers_class = transformers.CLIPProcessor
    transformers.CLIPProcessor = Stub
    try:
        for failure in (ImportError("no torchvision"), RuntimeError("torchvision is broken")):
            Stub.failure, Stub.calls = failure, []
            noise = io.StringIO()
            with contextlib.redirect_stdout(noise):
                got = f.load_processor(True)
            check(got == "standard processor" and Stub.calls == [fast_kwargs, {"use_fast": False}],
                  f"when the fast one fails with {type(failure).__name__}, it falls back to the standard one")
            check(("isn't available" in noise.getvalue()) == isinstance(failure, RuntimeError),
                  "...and say so only when the failure is unexpected (not for a plain missing torchvision)")
    finally:
        transformers.CLIPProcessor = real_transformers_class

    if have_torchvision:
        standard = transformers.CLIPProcessor.from_pretrained(f.MODEL_ID, local_files_only=True, use_fast=False)
        check(not is_fast(standard), "the standard processor really is the standard one")
        cosines = []
        for name_, array in synthetic_images().items():
            path = os.path.join(tmp, f"fp_{name_}.png")
            Image.fromarray(array).save(path)
            fast_f = f.encode_image_features(model, pixels_of(f, tmp, path, processor))
            slow_f = f.encode_image_features(model, pixels_of(f, tmp, path, standard))
            cosines.append(float((fast_f * slow_f).sum()))
        check(min(cosines) > 0.999, f"fast and standard processors give the same embeddings (worst cosine {min(cosines):.5f})")
    else:
        print("  skip - the fast-vs-standard comparison needs torchvision, which isn't installed here")

    small = Image.new("RGB", (400, 300))
    check(f.shrink_if_huge(small) is small, "an ordinary map is left as it is")
    huge = Image.new("RGB", (9000, 8000))                     # 72 megapixels, over the 60 million line
    shrunk = f.shrink_if_huge(huge)
    ratio = (shrunk.width / shrunk.height) / (huge.width / huge.height)
    check(shrunk.width * shrunk.height <= 6_000_000 and abs(ratio - 1) < 0.01,
          f"a 72-megapixel map is shrunk to {shrunk.width}x{shrunk.height}, same shape")


def test_wrong_python_is_explained():
    print("\n[wrong Python] a script started without the project's packages says what to do")
    import subprocess
    code = (
        "import runpy, sys\n"
        "results = {}\n"
        "def attempt(script):\n"
        "    sys.argv = [script]\n"
        "    try:\n"
        "        runpy.run_path(script, run_name='__main__')\n"
        "        results[script] = 'ran'\n"
        "    except SystemExit as e:\n"
        "        results[script] = str(e.code)\n"
        "sys.modules['numpy'] = None\n"
        "attempt('fgu_ui.py')\n"
        "del sys.modules['numpy']\n"
        "sys.modules['pillow_heif'] = None\n"
        "for script in ('fgu_find_maps.py', 'fgu_detect_grid.py'):\n"
        "    attempt(script)\n"
        "import json; print('RESULTS ' + json.dumps(results))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
                          env=dict(os.environ, PYTHONUTF8="1", PYTHONPATH=REPO_ROOT))
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULTS ")), None)
    results = __import__("json").loads(line[len("RESULTS "):]) if line else {}
    check(len(results) == 3, f"all three scripts were tried (got {sorted(results)}; stderr: {done.stderr[-200:]!r})")
    for script, message in sorted(results.items()):
        check("is missing" in message and "source venv/bin/activate" in message and "venv\\Scripts\\python.exe" in message,
              f"{script}: names the missing package and says to use the project's Python (Mac and Windows)")


def test_indexing_shows_progress(m, model, processor, tmp):
    print("\n[progress] indexing says what it is doing, so it never looks hung")
    import re
    f = m
    lib = os.path.join(tmp, "progress")
    os.makedirs(lib)
    for i in range(5):
        Image.fromarray(synthetic_images()["rooms"]).resize((160 + i, 120)).save(os.path.join(lib, f"m{i}.png"))

    saved = f.HEARTBEAT_SECONDS, f.HUGE_IMAGE_PIXELS, f.ANNOUNCE_LARGE_PIXELS
    f.HEARTBEAT_SECONDS, f.HUGE_IMAGE_PIXELS, f.ANNOUNCE_LARGE_PIXELS = 0, 10_000, 10_000   # every map counts as slow and as large
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            f.update_index(lib, batch_size=2)
    finally:
        f.HEARTBEAT_SECONDS, f.HUGE_IMAGE_PIXELS, f.ANNOUNCE_LARGE_PIXELS = saved
    text = out.getvalue()
    lines = text.splitlines()
    check(any("Loading the AI model" in l for l in lines), "it says it is loading the model before the silent part")
    check(any(re.search(r"\[2/5\] 40% . [\d.]+ maps/s . about .+ left", l) for l in lines), "after each batch: count, percent, speed and time left")
    check(any(re.search(r"\[5/5\] 100% . [\d.]+ maps/s . finished in", l) for l in lines), "the last line says it finished and how long it took")
    check(sum("large map (" in l for l in lines) == 5, "every very large map is announced before it is read")
    fresh = os.path.join(tmp, "progress_quiet")                                   # maps the model has not seen yet
    os.makedirs(fresh)
    for i in range(3):
        Image.fromarray(synthetic_images()["rooms"]).resize((170 + i, 120)).save(os.path.join(fresh, f"q{i}.png"))
    f.HUGE_IMAGE_PIXELS = 10_000                                                  # "big" for shrinking, but under the announce line
    quiet = io.StringIO()
    try:
        with contextlib.redirect_stdout(quiet):
            f.update_index(fresh, batch_size=2)
    finally:
        f.HUGE_IMAGE_PIXELS = saved[1]
    check("3 to read" in quiet.getvalue() and "large map (" not in quiet.getvalue(),
          "a big map that is not huge (under 200 megapixels) is read without an announcement")
    check(any("still working: " in l for l in lines), "a slow stretch produces a 'still working' line")
    check(f._duration(40) == "40 s" and f._duration(600) == "10 min" and f._duration(4800) == "1 h 20 min", "durations read naturally")


def main():
    m = load_module()
    loaded = m.load_model()
    if loaded is None:
        sys.exit(2)
    model, processor = loaded

    with tempfile.TemporaryDirectory() as tmp:
        test_new_path_matches_the_original_forward_pass(m, model, processor, tmp)
        test_unreadable_file_costs_only_itself(m, tmp)
        test_find_maps_ranks_sensibly(m, model, processor, tmp)
        test_huge_maps(m, model, processor, tmp)
        test_fast_image_processor(m, model, processor, tmp)
        test_wrong_python_is_explained()
        test_indexing_shows_progress(m, model, processor, tmp)

    print()
    if failures:
        print(f"❌ {len(failures)} check(s) failed:")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print("✅ All model correctness checks passed.")


if __name__ == "__main__":
    main()
