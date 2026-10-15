import cv2
import numpy as np
import pytest

from detection import lowlight as L


def dark_scene(seed=0):
    rng = np.random.default_rng(seed)
    img = np.full((240, 320, 3), 25, np.uint8)
    cv2.rectangle(img, (60, 80), (200, 200), (45, 50, 55), -1)
    noise = rng.normal(0, 4, img.shape)
    return np.clip(img + noise, 0, 255).astype(np.uint8)


def bright_scene():
    img = np.full((240, 320, 3), 150, np.uint8)
    cv2.rectangle(img, (60, 80), (200, 200), (90, 120, 160), -1)
    return img


def test_assessment_flags_dark_and_not_bright():
    assert L.assess_image(dark_scene())["is_low_light"] is True
    assert L.assess_image(bright_scene())["is_low_light"] is False


def test_enhancement_brightens_and_preserves_shape_and_original():
    d = dark_scene()
    before = d.copy()
    out, info = L.enhance(d)
    assert out.shape == d.shape and out.dtype == np.uint8
    assert np.array_equal(d, before), "original must never be modified"
    assert info["mean_luma_after"] > info["mean_luma_before"] + 20


def test_modes_off_auto_force():
    dark, bright = L.assess_image(dark_scene()), L.assess_image(bright_scene())
    assert L.should_enhance("off", dark)[0] is False
    assert L.should_enhance("auto", dark)[0] is True
    assert L.should_enhance("auto", bright)[0] is False          # daylight is left alone
    assert L.should_enhance("force", bright)[0] is True
    with pytest.raises(ValueError):
        L.should_enhance("night-vision", dark)


def test_overexposed_not_brightened_in_auto_and_force():
    over = np.full((200, 200, 3), 252, np.uint8)
    a = L.assess_image(over)
    assert a["is_overexposed"] and L.should_enhance("force", a)[0] is False


def test_empty_frame_rejected():
    with pytest.raises(ValueError):
        L.enhance(np.zeros((0, 0, 3), np.uint8))


def test_merge_views_keeps_provenance():
    from detection import detector as D
    o = [{"class_name": "person", "confidence": 0.5, "bbox": (10, 10, 50, 100)}]
    e = [{"class_name": "person", "confidence": 0.7, "bbox": (11, 10, 51, 100)}, {"class_name": "truck", "confidence": 0.6, "bbox": (200, 50, 300, 120)}]
    m = D.merge_views(o, e)
    views = {d["class_name"]: d["view"] for d in m}
    assert views == {"person": "both", "truck": "enhanced"} and len(m) == 2
