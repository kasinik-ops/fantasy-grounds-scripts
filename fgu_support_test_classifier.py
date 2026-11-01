#!/usr/bin/env python3
"""
Correctness tests for the model code in fgu_classify_maps.py and fgu_find_maps.py
(the safety tests in fgu_support_test_safety.py cover what they do to files; these
cover whether they still give the right answers).

The classifier encodes the category prompts once per run and pushes images through
the model in batches, instead of re-encoding every prompt for every image. That is
only acceptable if it gives the SAME answers, so this checks, against the original
one-image-at-a-time model(**inputs) forward pass:

  - identical probabilities and identical winning category, image by image
  - the same results whatever the batch size
  - a file that can't be read costs only itself, not the rest of its batch
  - fgu_find_maps.py encodes text and images exactly as the classifier does, and a
    search for a colour ranks the image of that colour first

It uses the real CLIP model from the local cache (it never goes online), so run it
the same way as the safety tests -- inside the project's venv, after the classifier
has been run once or the model has otherwise been downloaded:

    source venv/bin/activate
    python3 fgu_support_test_classifier.py

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
    """Import fgu_classify_maps without it parsing THIS script's command line."""
    sys.argv = ["fgu_classify_maps.py"]
    sys.modules.pop("fgu_classify_maps", None)
    return importlib.import_module("fgu_classify_maps")


def test_new_path_matches_the_original_forward_pass(m, model, processor, tmp):
    print("\n[model] cached-prompt batched path == the original per-image forward pass")
    prompts = list(m.CATEGORIES.values())
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

    pixels = torch.cat([m.preprocess_image(processor, p) for p in paths.values()])
    image_features = m.encode_image_features(model, pixels)
    new = m.softmax_probs(model, image_features, text_features).cpu()

    for i, name in enumerate(paths):
        diff = (new[i] - old[name]).abs().max().item()
        check(diff < 1e-3, f"{name}: probabilities match (largest difference {diff:.2e})")
        check(int(new[i].argmax()) == int(old[name].argmax()), f"{name}: same winning category")

    one_at_a_time = torch.cat([
        m.softmax_probs(model, m.encode_image_features(model, m.preprocess_image(processor, p)), text_features).cpu()
        for p in paths.values()
    ])
    check((one_at_a_time - new).abs().max().item() < 1e-4, "batching doesn't change any result (batch of 5 vs one at a time)")


def test_unreadable_file_costs_only_itself(m, tmp):
    print("\n[run] an unreadable image doesn't stop the rest of its batch")
    folder = os.path.join(tmp, "mixed")
    os.makedirs(folder)
    good = []
    for name, array in list(synthetic_images().items())[:3]:
        path = os.path.join(folder, f"{name}.png")
        Image.fromarray(array).save(path)
        good.append(path)
    bad = os.path.join(folder, "broken.png")
    with open(bad, "wb") as f:
        f.write(b"this is not a png")
    everything = sorted(good + [bad])
    entries = [(p, None, None) for p in everything]

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = m.run_classification(entries, no_update_check=True, batch_size=8)
    text = out.getvalue()
    check(len(result) == len(entries), "every file is accounted for in the result")
    check(text.count("Tagged:") == 3, f"the 3 good images were all tagged (saw {text.count('Tagged:')})")
    check(text.count("❌ Error") == 1, f"exactly one error was reported (saw {text.count('❌ Error')})")


def test_find_maps_agrees_with_the_classifier_and_ranks_sensibly(m, model, processor, tmp):
    print("\n[fgu_find_maps.py] same encoding as the classifier; a colour search finds that colour")
    sys.modules.pop("fgu_find_maps", None)
    f = importlib.import_module("fgu_find_maps")

    prompts = ["a tavern interior", "a dungeon corridor"]
    check((f.encode_text_features(model, processor, prompts) - m.encode_text_features(model, processor, prompts)).abs().max().item() < 1e-6,
          "text embeddings are identical to the classifier's")
    path = os.path.join(tmp, "probe.png")
    Image.fromarray(synthetic_images()["rooms"]).save(path)
    pixels = m.preprocess_image(processor, path)
    check((f.encode_image_features(model, pixels) - m.encode_image_features(model, pixels)).abs().max().item() < 1e-6,
          "image embeddings are identical to the classifier's")

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


def main():
    m = load_module()
    from transformers import CLIPModel, CLIPProcessor
    try:
        model = CLIPModel.from_pretrained(m.MODEL_ID, local_files_only=True).to(m.DEVICE)
        processor = CLIPProcessor.from_pretrained(m.MODEL_ID, local_files_only=True)
    except Exception as e:
        print(f"❌ The CLIP model isn't in the local cache ({e}).\n"
              f"   Run fgu_classify_maps.py once, with internet, to download it.")
        sys.exit(2)

    with tempfile.TemporaryDirectory() as tmp:
        test_new_path_matches_the_original_forward_pass(m, model, processor, tmp)
        test_unreadable_file_costs_only_itself(m, tmp)
        test_find_maps_agrees_with_the_classifier_and_ranks_sensibly(m, model, processor, tmp)

    print()
    if failures:
        print(f"❌ {len(failures)} check(s) failed:")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print("✅ All classifier correctness checks passed.")


if __name__ == "__main__":
    main()
