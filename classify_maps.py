import os
import argparse
import subprocess
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
    "indoors, church": "a top-down TTRPG battle map of an indoor church, cathedral, or temple",
    "indoors, dungeon": "a top-down TTRPG battle map of an indoor dungeon or stone cells",
    "indoors, tavern": "a top-down TTRPG battle map of an indoor tavern or inn",
    "outdoors, ruin": "a top-down TTRPG battle map of outdoor ancient stone ruins",
    "outdoors, forest": "a top-down TTRPG battle map of an outdoor forest or wilderness path",
}

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
            assigned_tag = keywords[best_idx]
            confidence = probs[0][best_idx].item() * 100

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

# --- PASS 2: FGU GRID DETECTION STUB ---
def run_pass_2(image_paths):
    print("\n--- Running Pass 2: Fantasy Grounds Grid Alignment ---")
    print("⚠️  Pass 2 is not yet implemented — no XML sidecar files will be written.")
    for idx, (path, _) in enumerate(image_paths, start=1):
        # We will insert the OpenCV grid detection algorithm here next!
        print(f"[{idx}/{len(image_paths)}] Skipping grid detection for {os.path.basename(path)} (not implemented)")

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
