"""
Low-light assessment + enhancement for uploaded images and sampled video frames.

HONEST LIMITS (also shown in the UI and the evidence PDF):
  * This is *software contrast enhancement* (denoise -> gamma -> CLAHE). It is NOT
    thermal / infrared / true night-vision hardware.
  * It can only amplify what the sensor actually captured. In near-total darkness
    it mostly amplifies noise, and it can create false detections as well as
    reveal real ones. More detections after enhancement is NOT proof of better
    accuracy - the pipeline therefore keeps original and enhanced results
    separate and labelled.
"""
import logging
import math

import cv2
import numpy as np

import config

logger = logging.getLogger(__name__)

MODES = ("off", "auto", "force")


def _luma(frame):
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def estimate_noise_sigma(gray):
    """Immerkaer (1996) fast noise-sigma estimate on a (down-scaled) grayscale image."""
    h, w = gray.shape[:2]
    if max(h, w) > 640:
        s = 640.0 / max(h, w)
        gray = cv2.resize(gray, (max(3, int(w * s)), max(3, int(h * s))), interpolation=cv2.INTER_AREA)
        h, w = gray.shape[:2]
    if h < 3 or w < 3:
        return 0.0
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float32)
    conv = cv2.filter2D(gray.astype(np.float32), -1, k)[1:-1, 1:-1]
    return float(math.sqrt(math.pi / 2.0) * np.abs(conv).sum() / (6.0 * (w - 2) * (h - 2)))


def assess_image(frame):
    """Objective brightness / noise / blur statistics for one BGR frame."""
    gray = _luma(frame)
    mean_luma = float(gray.mean())
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    noise = estimate_noise_sigma(gray)
    dark_fraction = float((gray < 40).mean())
    over_fraction = float((gray > 245).mean())
    is_low = mean_luma < config.LOWLIGHT_DARK_LUMA_THRESHOLD
    return {
        "mean_luma": round(mean_luma, 1),
        "p5_luma": round(float(np.percentile(gray, 5)), 1),
        "p95_luma": round(float(np.percentile(gray, 95)), 1),
        "dark_pixel_fraction": round(dark_fraction, 3),
        "overexposed_pixel_fraction": round(over_fraction, 3),
        "noise_sigma": round(noise, 2),
        "blur_laplacian_var": round(blur, 1),
        "is_low_light": bool(is_low),
        "is_overexposed": bool(over_fraction > 0.25),
        "is_noisy": bool(noise > 6.0),
        "is_blurry": bool(blur < 40.0),
    }


def enhance(frame, assessment=None):
    """
    Returns (enhanced_bgr_uint8, info). Steps: optional denoise -> adaptive gamma on
    LAB-L -> CLAHE on LAB-L. Never modifies the input array.
    """
    if frame is None or frame.size == 0:
        raise ValueError("Cannot enhance an empty frame")
    a = assessment or assess_image(frame)
    steps = []
    out = frame.copy()
    h, w = out.shape[:2]

    if a["is_noisy"]:
        if max(h, w) <= 1280:
            out = cv2.fastNlMeansDenoisingColored(out, None, h=min(10, max(3, a["noise_sigma"])), hColor=5,
                                                  templateWindowSize=7, searchWindowSize=15)
            steps.append("nl_means_denoise")
        else:
            out = cv2.bilateralFilter(out, d=7, sigmaColor=40, sigmaSpace=40)
            steps.append("bilateral_denoise")

    lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
    l_chan, a_chan, b_chan = cv2.split(lab)

    mean_l = max(1.0, float(l_chan.mean()))
    gamma = 1.0
    if mean_l < 110.0:
        gamma = math.log(110.0 / 255.0) / math.log(mean_l / 255.0)
        gamma = float(min(1.0, max(0.4, gamma)))
    if gamma < 0.999:
        lut = np.array([255.0 * ((i / 255.0) ** gamma) for i in range(256)], dtype=np.uint8)
        l_chan = cv2.LUT(l_chan, lut)
        steps.append(f"gamma={gamma:.2f}")

    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    l_chan = clahe.apply(l_chan)
    steps.append("clahe(clip=2.5,tile=8x8)")

    out = cv2.cvtColor(cv2.merge([l_chan, a_chan, b_chan]), cv2.COLOR_LAB2BGR)
    after = float(_luma(out).mean())
    return out, {
        "steps": steps,
        "gamma": round(gamma, 3),
        "mean_luma_before": a["mean_luma"],
        "mean_luma_after": round(after, 1),
    }


def should_enhance(mode, assessment):
    """off -> never; force -> always (unless overexposed); auto -> only when measured dark."""
    mode = (mode or "off").lower()
    if mode not in MODES:
        raise ValueError(f"Invalid low-light mode '{mode}'. Allowed: {list(MODES)}")
    if mode == "off":
        return False, "low-light enhancement disabled"
    if assessment["is_overexposed"] and not assessment["is_low_light"]:
        return False, "image is overexposed - brightening would make it worse"
    if mode == "force":
        return True, "forced by operator"
    if assessment["is_low_light"]:
        return True, f"measured mean luma {assessment['mean_luma']} < {config.LOWLIGHT_DARK_LUMA_THRESHOLD:g}"
    return False, f"image is not dark (mean luma {assessment['mean_luma']}) - enhancement skipped"
