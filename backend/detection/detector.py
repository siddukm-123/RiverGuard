"""
YOLOv8 object detector - real inference, no stubs.

What the default model can and cannot do
----------------------------------------
Pretrained COCO weights (yolov8n.pt) know 'person' (0) and 'truck' (7); 'car' (2) and
'bus' (5) are reported as separate *context* classes. COCO has NO excavator/JCB class,
so excavators are only detected when a custom model is supplied through
CUSTOM_JCB_MODEL_PATH. Until then `get_model_status()["excavator_detection_available"]`
is False and the UI/PDF say so - the pipeline never pretends otherwise.

Detections carry provenance: which model produced them (`model`), the raw class name
(`raw_class`), whether they count as evidence or only context (`role`) and, when
low-light enhancement is used, which view they came from (`view`: original/enhanced/both).
"""
import hashlib
import logging
import threading
import time
from pathlib import Path

import config
from . import lowlight

logger = logging.getLogger(__name__)

_base_model = None
_custom_model = None
_load_error = None
_load_lock = threading.Lock()
_infer_lock = threading.Lock()  # ultralytics predict() is not guaranteed thread-safe on one instance
_weights_sha_cache = {}

COCO_PERSON, COCO_CAR, COCO_BUS, COCO_TRUCK = 0, 2, 5, 7
EVIDENCE_CLASSES = {"truck", "jcb", "person"}  # classes the risk scorer is allowed to use
MAX_IMAGE_PIXELS = 100_000_000  # refuse decompression-bomb style images


class ModelUnavailableError(RuntimeError):
    """Raised when YOLO weights cannot be loaded. Surfaces as a clear 503, never a fake result."""


# ── model management ────────────────────────────────────────────────────
def _sha256_file(path):
    path = str(path)
    if path in _weights_sha_cache:
        return _weights_sha_cache[path]
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _weights_sha_cache[path] = h.hexdigest()
    except OSError:
        _weights_sha_cache[path] = None
    return _weights_sha_cache[path]


def _load_models():
    global _base_model, _custom_model, _load_error
    if _base_model is not None:
        return
    with _load_lock:
        if _base_model is not None:
            return
        try:
            from ultralytics import YOLO
            logger.info(f"Loading YOLO model: {config.YOLO_MODEL_PATH}")
            _base_model = YOLO(config.YOLO_MODEL_PATH)
            _load_error = None
        except Exception as e:  # download failure, corrupt file, missing torch...
            _load_error = f"{type(e).__name__}: {e}"
            logger.error(f"YOLO base model failed to load: {_load_error}")
            raise ModelUnavailableError(f"YOLO weights '{config.YOLO_MODEL_PATH}' could not be loaded ({_load_error})")

        custom = config.CUSTOM_JCB_MODEL_PATH
        if custom:
            if Path(custom).exists():
                try:
                    from ultralytics import YOLO
                    logger.info(f"Loading custom excavator model: {custom}")
                    _custom_model = YOLO(custom)
                except Exception as e:
                    logger.error(f"Custom model '{custom}' failed to load: {e}")
            else:
                logger.warning(f"CUSTOM_JCB_MODEL_PATH='{custom}' does not exist - excavator detection stays OFF.")


def _excavator_names(model):
    names = getattr(model, "names", {}) or {}
    return sorted(n for n in names.values() if any(k in str(n).lower() for k in config.EXCAVATOR_CLASS_KEYWORDS))


def get_model_status(load=False):
    """Truthful description of what the detector can do right now (used by /api/model-status, UI and PDF)."""
    if load:
        try:
            _load_models()
        except ModelUnavailableError:
            pass
    custom_path = config.CUSTOM_JCB_MODEL_PATH or ""
    exc_names = _excavator_names(_custom_model) if _custom_model is not None else []
    return {
        "base_model_path": config.YOLO_MODEL_PATH,
        "base_model_loaded": _base_model is not None,
        "base_model_error": _load_error,
        "base_classes_used": ["person", "truck"] + (["car", "bus (context only)"]),
        "custom_model_path": custom_path or None,
        "custom_model_file_exists": bool(custom_path and Path(custom_path).exists()),
        "custom_model_loaded": _custom_model is not None,
        "custom_model_classes": sorted(str(v) for v in (getattr(_custom_model, "names", {}) or {}).values()) if _custom_model else [],
        "excavator_classes_recognised": exc_names,
        "excavator_detection_available": bool(exc_names),
        "fold_vehicles_as_truck": config.FOLD_VEHICLES_AS_TRUCK,
        "conf_threshold": config.DETECTION_CONF_THRESHOLD,
    }


def get_model_info():
    """Provenance block stored with every alert / printed in the evidence PDF."""
    try:
        import ultralytics
        ver = ultralytics.__version__
    except Exception:
        ver = None
    base_sha = _sha256_file(config.YOLO_MODEL_PATH) if Path(config.YOLO_MODEL_PATH).exists() else None
    info = {
        "framework": "ultralytics",
        "framework_version": ver,
        "base_model": config.YOLO_MODEL_PATH,
        "base_model_sha256": base_sha,
        "custom_model": config.CUSTOM_JCB_MODEL_PATH or None,
        "custom_model_sha256": _sha256_file(config.CUSTOM_JCB_MODEL_PATH) if _custom_model is not None else None,
        "conf_threshold": config.DETECTION_CONF_THRESHOLD,
        "excavator_detection_available": bool(_custom_model is not None and _excavator_names(_custom_model)),
    }
    return info


# ── geometry helpers ────────────────────────────────────────────────────
def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _dedupe_same_class(dets, iou_thr=0.6):
    """Cross-model NMS: base + custom model may both report the same truck/person."""
    dets = sorted(dets, key=lambda d: d["confidence"], reverse=True)
    kept = []
    for d in dets:
        if any(k["class_name"] == d["class_name"] and _iou(k["bbox"], d["bbox"]) >= iou_thr for k in kept):
            continue
        kept.append(d)
    return kept


# ── single-frame detection ──────────────────────────────────────────────
def detect_frame(frame, conf_threshold=None):
    """
    Real inference on one BGR frame (numpy array as read by OpenCV).
    Returns [{class_name, raw_class, confidence, bbox:(x1,y1,x2,y2), model, role}].
    """
    import numpy as np
    if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
        raise ValueError("detect_frame() needs a non-empty image array")
    _load_models()
    conf = config.DETECTION_CONF_THRESHOLD if conf_threshold is None else conf_threshold
    results = []

    with _infer_lock:
        base_out = _base_model.predict(frame, conf=conf, verbose=False)[0]
    for box in base_out.boxes:
        cls_id = int(box.cls[0])
        if cls_id == COCO_PERSON:
            name, role = "person", "evidence"
        elif cls_id == COCO_TRUCK:
            name, role = "truck", "evidence"
        elif cls_id in (COCO_CAR, COCO_BUS):
            if config.FOLD_VEHICLES_AS_TRUCK:
                name, role = "truck", "evidence"
            else:
                name, role = ("car" if cls_id == COCO_CAR else "bus"), "context"
        else:
            continue
        x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
        results.append({
            "class_name": name, "raw_class": str(base_out.names.get(cls_id, cls_id)),
            "confidence": round(float(box.conf[0]), 3), "bbox": (x1, y1, x2, y2),
            "model": "base", "role": role,
        })

    if _custom_model is not None:
        with _infer_lock:
            cust_out = _custom_model.predict(frame, conf=conf, verbose=False)[0]
        for box in cust_out.boxes:
            raw = str(cust_out.names.get(int(box.cls[0]), int(box.cls[0]))).lower()
            if any(k in raw for k in config.EXCAVATOR_CLASS_KEYWORDS):
                name, role = "jcb", "evidence"
            elif raw in ("truck", "dump truck", "dump_truck", "tipper"):
                name, role = "truck", "evidence"
            elif raw in ("person", "worker"):
                name, role = "person", "evidence"
            else:
                name, role = raw, "context"  # never silently relabel an unrelated class as 'jcb'
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
            results.append({
                "class_name": name, "raw_class": raw, "confidence": round(float(box.conf[0]), 3),
                "bbox": (x1, y1, x2, y2), "model": "custom", "role": role,
            })

    return _dedupe_same_class(results)


# ── low-light aware detection ───────────────────────────────────────────
def merge_views(original, enhanced, iou_thr=0.5):
    """
    Union of detections from the original and the enhanced frame, de-duplicated, with
    provenance: view = 'original' | 'enhanced' | 'both'. Never loses the distinction.
    """
    merged = []
    for d in original:
        d = dict(d)
        d["view"] = "original"
        merged.append(d)
    for e in enhanced:
        match = next((m for m in merged if m["class_name"] == e["class_name"] and _iou(m["bbox"], e["bbox"]) >= iou_thr), None)
        if match is not None:
            match["view"] = "both"
            match["enhanced_confidence"] = e["confidence"]
        else:
            e = dict(e)
            e["view"] = "enhanced"
            merged.append(e)
    return merged


def detect_frame_lowlight(frame, mode="off", conf_threshold=None):
    """
    Returns dict:
      detections        merged, view-tagged list (what the risk pipeline uses)
      original / enhanced   raw per-view lists (enhanced is None when not applied)
      enhanced_frame    BGR array or None
      lowlight          {mode, applied, reason, assessment, enhancement}
    """
    assessment = lowlight.assess_image(frame)
    apply, reason = lowlight.should_enhance(mode, assessment)
    original = detect_frame(frame, conf_threshold)
    info = {"mode": (mode or "off"), "applied": False, "reason": reason, "assessment": assessment, "enhancement": None}
    if not apply:
        for d in original:
            d["view"] = "original"
        return {"detections": original, "original": original, "enhanced": None, "enhanced_frame": None, "lowlight": info}

    enhanced_frame, enh_info = lowlight.enhance(frame, assessment)
    enhanced = detect_frame(enhanced_frame, conf_threshold)
    info["applied"] = True
    info["enhancement"] = enh_info
    return {"detections": merge_views(original, enhanced), "original": original, "enhanced": enhanced,
            "enhanced_frame": enhanced_frame, "lowlight": info}


# ── file level ──────────────────────────────────────────────────────────
def read_image_file(image_path):
    """Decode + validate an image. Raises ValueError with an operator-friendly message."""
    import cv2
    import numpy as np
    p = Path(image_path)
    if not p.exists() or p.stat().st_size == 0:
        raise ValueError("Image file is missing or empty")
    data = np.fromfile(str(p), dtype=np.uint8)
    frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("File is not a readable image (corrupt or unsupported content)")
    h, w = frame.shape[:2]
    if h < 16 or w < 16:
        raise ValueError(f"Image is too small ({w}x{h}px)")
    if h * w > MAX_IMAGE_PIXELS:
        raise ValueError(f"Image is too large ({w}x{h}px)")
    return frame


def detect_image_file(image_path, conf_threshold=None, lowlight_mode="off"):
    """
    Backwards-compatible: returns (detections, frame).
    Use analyze_image_file() when the low-light comparison data is needed.
    """
    frame = read_image_file(image_path)
    return detect_frame(frame, conf_threshold), frame


def analyze_image_file(image_path, conf_threshold=None, lowlight_mode="off"):
    frame = read_image_file(image_path)
    t0 = time.time()
    res = detect_frame_lowlight(frame, lowlight_mode, conf_threshold)
    res["frame"] = frame
    res["inference_seconds"] = round(time.time() - t0, 2)
    res["image_size"] = {"width": int(frame.shape[1]), "height": int(frame.shape[0])}
    return res


def analyze_video_file(video_path, sample_fps=None, conf_threshold=None, lowlight_mode="off"):
    """
    Frame-sampled detection + tracking over a video.

    Returns dict with: detections (all frames, with frame_number + timestamp_s + track_id),
    track_summaries, video_fps, duration_s, frames_analyzed, truncated, best_frame
    (the sampled frame with the most evidence objects, plus its detections / enhanced twin),
    lowlight summary.
    """
    import cv2
    from .tracker import SimpleTracker

    p = Path(video_path)
    if not p.exists() or p.stat().st_size == 0:
        raise ValueError("Video file is missing or empty")
    sample_fps = sample_fps or config.VIDEO_SAMPLE_FPS
    cap = cv2.VideoCapture(str(p))
    if not cap.isOpened():
        raise ValueError("File is not a readable video (corrupt or unsupported codec)")

    video_fps = cap.get(cv2.CAP_PROP_FPS)
    if not video_fps or video_fps != video_fps or video_fps < 1 or video_fps > 240:
        video_fps = 25.0
    frame_interval = max(1, int(round(video_fps / sample_fps)))
    max_frames = int(config.VIDEO_MAX_SECONDS * video_fps)

    tracker = SimpleTracker()
    all_dets = []
    best = None  # (score, frame_idx, frame, dets, enhanced_frame)
    frame_idx = 0
    analyzed = 0
    enhanced_frames = 0
    n_orig = n_enh_only = 0
    luma_samples = []
    truncated = False
    frame_wh = None

    while True:
        if not cap.grab():
            break
        if frame_idx >= max_frames:
            truncated = True
            break
        if frame_idx % frame_interval == 0:
            ret, frame = cap.retrieve()
            if ret and frame is not None:
                frame_wh = (frame.shape[1], frame.shape[0])
                res = detect_frame_lowlight(frame, lowlight_mode, conf_threshold)
                dets = tracker.update(frame_idx, res["detections"], frame_size=frame_wh)
                ts = round(frame_idx / video_fps, 2)
                for d in dets:
                    d["frame_number"] = frame_idx
                    d["timestamp_s"] = ts
                all_dets.extend(dets)
                analyzed += 1
                luma_samples.append(res["lowlight"]["assessment"]["mean_luma"])
                if res["lowlight"]["applied"]:
                    enhanced_frames += 1
                n_orig += len([d for d in dets if d.get("view") in ("original", "both")])
                n_enh_only += len([d for d in dets if d.get("view") == "enhanced"])
                evidence = [d for d in dets if d.get("role") == "evidence"]
                score = (len(evidence), sum(d["confidence"] for d in evidence))
                if best is None or score > best[0]:
                    best = (score, frame_idx, frame.copy(), [dict(d) for d in dets], res["enhanced_frame"])
        frame_idx += 1

    cap.release()
    if analyzed == 0:
        raise ValueError("Video contains no readable frames")

    track_summaries = tracker.finalize(video_fps=video_fps, frame_size=frame_wh)
    return {
        "detections": all_dets,
        "track_summaries": track_summaries,
        "video_fps": round(video_fps, 2),
        "duration_s": round(frame_idx / video_fps, 1),
        "frames_analyzed": analyzed,
        "sample_fps": sample_fps,
        "truncated": truncated,
        "max_seconds": config.VIDEO_MAX_SECONDS,
        "best_frame": None if best is None else {
            "frame_number": best[1], "timestamp_s": round(best[1] / video_fps, 2),
            "frame": best[2], "detections": best[3], "enhanced_frame": best[4],
        },
        "lowlight": {
            "mode": lowlight_mode or "off",
            "frames_enhanced": enhanced_frames,
            "frames_analyzed": analyzed,
            "mean_luma_min": round(min(luma_samples), 1) if luma_samples else None,
            "mean_luma_avg": round(sum(luma_samples) / len(luma_samples), 1) if luma_samples else None,
            "detections_in_original_view": n_orig,
            "detections_only_in_enhanced_view": n_enh_only,
        },
    }


def detect_video_file(video_path, sample_fps=None, conf_threshold=None):
    """Backwards-compatible 4-tuple: (all_detections, track_summaries, video_fps, thumbnail_frame)."""
    r = analyze_video_file(video_path, sample_fps, conf_threshold, "off")
    thumb = r["best_frame"]["frame"] if r["best_frame"] else None
    return r["detections"], r["track_summaries"], r["video_fps"], thumb


# ── drawing ─────────────────────────────────────────────────────────────
_COLORS = {"truck": (0, 165, 255), "jcb": (0, 0, 255), "person": (0, 255, 0), "car": (160, 160, 160), "bus": (160, 160, 160)}


def draw_detections(frame, detections, title=None):
    """Returns a copy of `frame` with boxes + labels. Only pass detections belonging to THIS frame."""
    import cv2
    out = frame.copy()
    for d in detections:
        x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
        color = _COLORS.get(d["class_name"], (255, 255, 255))
        thickness = 2 if d.get("role", "evidence") == "evidence" else 1
        cv2.rectangle(out, (x1, y1), (x2, y2), color, thickness)
        label = f"{d['class_name']} {d['confidence']:.2f}"
        if d.get("track_id") is not None:
            label = f"#{d['track_id']} " + label
        if d.get("view") == "enhanced":
            label += " [enh]"
        cv2.putText(out, label, (x1, max(12, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
    if title:
        cv2.putText(out, title, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
        cv2.putText(out, title, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 0), 1)
    return out


def summarize_counts(detections):
    """{'person': 3, 'truck': 1, ...} over a list of detections."""
    counts = {}
    for d in detections:
        counts[d["class_name"]] = counts.get(d["class_name"], 0) + 1
    return counts
