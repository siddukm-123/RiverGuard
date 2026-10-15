"""
Compare detections on original vs enhanced versions of every image in a folder. Prints COUNTS only.
It does NOT measure accuracy: with no ground-truth labels, extra detections may be false positives.
Usage: python tools/evaluate_lowlight.py path/to/night_images [--model yolov8n.pt]
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import config
from detection import detector

ap = argparse.ArgumentParser(); ap.add_argument("folder"); ap.add_argument("--model")
a = ap.parse_args()
if a.model: config.YOLO_MODEL_PATH = a.model
print(f"{'image':40} {'orig':>5} {'enh':>5} {'merged':>6}  applied")
for p in sorted(Path(a.folder).iterdir()):
    if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp"): continue
    try:
        r = detector.analyze_image_file(p, lowlight_mode="force")
    except ValueError as e:
        print(f"{p.name:40} skipped: {e}"); continue
    n = lambda L: len([d for d in (L or []) if d["role"] == "evidence"])
    print(f"{p.name:40} {n(r['original']):5} {n(r['enhanced']):5} {n(r['detections']):6}  {r['lowlight']['applied']}")
