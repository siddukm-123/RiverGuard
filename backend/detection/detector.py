"""
YOLOv8 object detector — real detection, not a stub. Uses Ultralytics'
pretrained COCO weights by default, which reliably detects 'person' and
'truck' out of the box (COCO classes 0 and 7).

Honest limitation: COCO has no 'JCB'/excavator class, so out of the box
this will NOT detect JCBs/excavators. To add that, fine-tune a YOLOv8
model on an excavator dataset (e.g. Roboflow has several public ones),
drop the resulting best.pt in models/, and set CUSTOM_JCB_MODEL_PATH in
.env — this module will then run both models and merge results. Until
then, the pipeline works correctly on truck + person detection alone,
which is still a strong signal when combined with clustering + zone checks.
"""
import logging
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_yolo_model = None
_jcb_model = None

COCO_CLASSES_OF_INTEREST = {2: "vehicle", 5: "bus", 7: "truck", 0: "person"}


def _load_models():
    global _yolo_model, _jcb_model
    if _yolo_model is not None:
        return
    from ultralytics import YOLO
    logger.info(f"Loading YOLO model: {config.YOLO_MODEL_PATH}")
    _yolo_model = YOLO(config.YOLO_MODEL_PATH)

    if config.CUSTOM_JCB_MODEL_PATH and Path(config.CUSTOM_JCB_MODEL_PATH).exists():
        logger.info(f"Loading custom JCB/excavator model: {config.CUSTOM_JCB_MODEL_PATH}")
        _jcb_model = YOLO(config.CUSTOM_JCB_MODEL_PATH)


def detect_frame(frame, conf_threshold=None):
    """
    Runs detection on a single frame (numpy array, BGR — as read by OpenCV).
    Returns a list of dicts: {class_name, confidence, bbox: (x1,y1,x2,y2)}.
    """
    _load_models()
    conf_threshold = conf_threshold or config.DETECTION_CONF_THRESHOLD
    results = []

    yolo_out = _yolo_model.predict(frame, conf=conf_threshold, verbose=False)[0]
    for box in yolo_out.boxes:
        cls_id = int(box.cls[0])
        if cls_id not in COCO_CLASSES_OF_INTEREST:
            continue
        class_name = COCO_CLASSES_OF_INTEREST[cls_id]
        if class_name in ("vehicle", "bus"):
            class_name = "truck"  # fold generic vehicle/bus into "truck" bucket for this use case
        x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
        results.append({
            "class_name": class_name,
            "confidence": round(float(box.conf[0]), 3),
            "bbox": (x1, y1, x2, y2),
        })

    if _jcb_model is not None:
        jcb_out = _jcb_model.predict(frame, conf=conf_threshold, verbose=False)[0]
        for box in jcb_out.boxes:
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
            results.append({
                "class_name": "jcb",
                "confidence": round(float(box.conf[0]), 3),
                "bbox": (x1, y1, x2, y2),
            })

    return results


def detect_image_file(image_path, conf_threshold=None):
    import cv2
    frame = cv2.imread(str(image_path))
    if frame is None:
        raise ValueError(f"Could not read image: {image_path}")
    detections = detect_frame(frame, conf_threshold)
    return detections, frame


def detect_video_file(video_path, sample_fps=None, conf_threshold=None):
    """
    Runs detection across a video, sampling at `sample_fps` frames/sec
    (not every frame — that would be far too slow on CPU) and feeding
    every sampled frame's detections through a SimpleTracker so each
    object gets a persistent track_id.

    Returns (all_detections, track_summaries, video_fps, sample_frame_for_thumbnail).
    """
    import cv2
    from .tracker import SimpleTracker

    sample_fps = sample_fps or config.VIDEO_SAMPLE_FPS
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"Could not open video: {video_path}")

    video_fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frame_interval = max(1, int(round(video_fps / sample_fps)))

    tracker = SimpleTracker()
    all_detections = []
    frame_idx = 0
    thumbnail = None

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % frame_interval == 0:
            dets = detect_frame(frame, conf_threshold)
            dets = tracker.update(frame_idx, dets)
            for d in dets:
                d["frame_number"] = frame_idx
            all_detections.extend(dets)
            if thumbnail is None and dets:
                thumbnail = frame.copy()
        frame_idx += 1

    cap.release()
    track_summaries = tracker.finalize(fps=sample_fps)
    return all_detections, track_summaries, video_fps, thumbnail


def draw_detections(frame, detections):
    """Returns a copy of `frame` with bounding boxes + labels drawn on it."""
    import cv2
    out = frame.copy()
    colors = {"truck": (0, 165, 255), "jcb": (0, 0, 255), "person": (0, 255, 0)}
    for d in detections:
        x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
        color = colors.get(d["class_name"], (255, 255, 255))
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = f"{d['class_name']} {d['confidence']:.2f}"
        if "track_id" in d:
            label = f"#{d['track_id']} " + label
        cv2.putText(out, label, (x1, max(0, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
    return out
