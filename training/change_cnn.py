"""
Inference wrapper for the trained Siamese CNN change detector.

Design mirrors detection/detector.py's pattern deliberately: lazy model
loading, a clear is_configured() check, and a graceful path when the
model file isn't present — so this slots into the existing codebase style
rather than introducing a new pattern.

This is meant to sit ALONGSIDE compute_ndwi_change() in satellite/gee_client.py,
not silently replace it: call this first, and if it returns None (model not
trained/deployed yet), fall back to the existing NDWI method exactly like
the app already falls back to synthetic data today. That keeps the app
fully functional at every stage of rolling this out.

Usage from gee_client.py's get_before_after_pair(), added as a new first
attempt before the existing compute_ndwi_change() call:

    from satellite.change_cnn import compute_cnn_change
    result = compute_cnn_change(before_thumb_path, after_thumb_path)
    if result is not None:
        return result
    result = compute_ndwi_change(lat, lng, before_date, after_date, buffer_m)
    ...
"""
import logging
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_model = None
_model_load_attempted = False


def _load_model():
    global _model, _model_load_attempted
    if _model_load_attempted:
        return _model
    _model_load_attempted = True

    model_path = getattr(config, "CHANGE_MODEL_PATH", "")
    if not model_path or not Path(model_path).exists():
        logger.info("CHANGE_MODEL_PATH not set or file missing — CNN change detection unavailable, will fall back to NDWI.")
        return None

    try:
        import torch
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "training"))
        from siamese_change_model import SiameseChangeDetector

        model = SiameseChangeDetector()
        model.load_state_dict(torch.load(model_path, map_location="cpu"))
        model.eval()
        _model = model
        logger.info(f"Loaded trained Siamese change detector from {model_path}")
        return _model
    except Exception as e:
        logger.error(f"Failed to load Siamese change detector: {e}")
        return None


def is_configured():
    return _load_model() is not None


def compute_cnn_change(before_image_path, after_image_path):
    """
    Runs the trained CNN on two local image files (already-downloaded
    before/after thumbnails — see download_thumbnail() in gee_client.py).

    Returns the same shape as compute_ndwi_change() so callers can treat
    them interchangeably:
        {"change_score": float, "synthetic": False, "method": "cnn"}
    or None if the model isn't available (caller should fall back to NDWI).
    """
    model = _load_model()
    if model is None:
        return None

    try:
        import torch
        from PIL import Image
        import torchvision.transforms as T

        transform = T.Compose([
            T.Resize((128, 128)),
            T.ToTensor(),  # scales to [0, 1] automatically for standard image files
        ])

        before_img = transform(Image.open(before_image_path).convert("RGB")).unsqueeze(0)
        after_img = transform(Image.open(after_image_path).convert("RGB")).unsqueeze(0)

        with torch.no_grad():
            score = model(before_img, after_img).item()

        return {"change_score": score, "synthetic": False, "method": "cnn"}
    except Exception as e:
        logger.error(f"CNN change inference failed: {e}")
        return None
