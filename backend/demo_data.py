"""
Test (demo) data: one river with a HIGH-severity alert AND its proof (evidence image, detection rows, hashed PDF),
so the dashboard can be demonstrated in every state.

Honesty rules (so test data can never be mistaken for a real incident):
  * the river name always ends with " (Test)"  -> the dashboard, feed and map popups show a TEST badge;
  * the alert is marked reasoning_signals.demo_test = true and its text starts with "TEST ALERT";
  * the evidence image is a GENERATED ILLUSTRATION that says so on the picture itself, and the evidence PDF opens with
    a "TEST ALERT - NOT A REAL DETECTION" banner (the PDF, its SHA-256 hash and the verify endpoint are the real ones);
  * the river geometry is the REAL channel of a real river: copied from a river you already monitor (no external call,
    so it is fast and cannot time out), or - only when you have none - mapped from OpenStreetMap;
  * the risk score is computed by the real rule engine (detection.reasoning) from the simulated inputs below;
  * the river is created with monitoring OFF and the scheduler never scans it automatically;
  * everything is removed again with remove().
"""
import copy
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config
import gcs_storage
from api.db_compat import insert_dropping_missing_columns
from api.supabase_client import table_delete, table_delete_where, table_insert, table_select
from detection import reasoning

logger = logging.getLogger(__name__)

DEMO_SUFFIX = " (Test)"
DEMO_FALLBACK_NAME = "Tungabhadra River"
DEMO_ANCHOR = (14.5126, 75.8050)          # used only if no monitored river is available (then snapped to the real channel via OSM)
DEMO_SEARCH_NAME = "Tungabhadra River"

# What the simulated scene "contains": a truck + an excavator + people working together, inside the buffer, with a truck
# loitering (loading). The REAL scorer turns this into the risk score / explanation.
SIMULATED_SIGNALS = {
    "input_type": "image",
    "inside_buffer": True,
    "location_source": "demo",
    "object_counts": {"truck": 2, "jcb": 1, "person": 3},
    "best_cluster": {"has_truck": True, "has_jcb": True, "has_person": True, "count": 6},
    "track_behaviors": [{"class_name": "truck", "behavior": "loitering"}],
    "model_info": {"excavator_detection_available": True},
}

OPTIONAL_RIVER_COLUMNS = ("monitor_points", "buffer_geojson")      # may be missing in a database created from an older migration

SCENE_W, SCENE_H = 1280, 720
# (class_name, raw_class, confidence, bbox) - the picture is drawn AROUND these boxes, so image and detections always agree.
SCENE_OBJECTS = [
    ("truck", "truck", 0.91, (96, 372, 452, 592)),
    ("truck", "truck", 0.87, (748, 408, 1084, 608)),
    ("jcb", "excavator", 0.89, (458, 318, 826, 604)),
    ("person", "person", 0.86, (418, 462, 468, 596)),
    ("person", "person", 0.82, (824, 470, 868, 600)),
    ("person", "person", 0.79, (1098, 458, 1146, 604)),
]


class DemoSeedError(RuntimeError):
    """Raised when the test data cannot be created. The message says which step failed and why."""


def is_demo_river(river):
    return str((river or {}).get("name", "")).endswith(DEMO_SUFFIX)


def demo_rivers():
    return [r for r in table_select("rivers") if is_demo_river(r)]


def _step(label, fn, *args, **kwargs):
    """Runs one step; any failure becomes a DemoSeedError that names the step (instead of a bare 'Internal server error')."""
    try:
        return fn(*args, **kwargs)
    except DemoSeedError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Test data: failed while {label}")
        raise DemoSeedError(f"Could not create the test data while {label}: {e.__class__.__name__}: {str(e)[:200]}")


def _points(river):
    pts = river.get("monitor_points") or []
    if isinstance(pts, str):
        try:
            pts = json.loads(pts)
        except ValueError:
            pts = []
    return [p for p in pts if isinstance(p, dict) and p.get("lat") is not None and p.get("lng") is not None]


def _pick_real_river():
    """A river the user already monitors, with a mapped corridor. Prefers the Tungabhadra (the project's home river)."""
    candidates = [r for r in table_select("rivers") if not is_demo_river(r) and len(_points(r)) >= 3]
    preferred = [r for r in candidates if "tungabhadra" in str(r.get("name", "")).lower()]
    return (preferred or candidates or [None])[0]


def _map_real_channel(snap_to_river_center, attempts=2):
    last = None
    for i in range(attempts):
        try:
            lat, lng, points = snap_to_river_center(DEMO_ANCHOR[0], DEMO_ANCHOR[1], river_name=DEMO_SEARCH_NAME)
            if points:
                return lat, lng, points
            last = "no river channel found near the anchor point"
        except Exception as e:  # noqa: BLE001 - Overpass is flaky; retry once, then explain
            last = f"{e.__class__.__name__}: {str(e)[:120]}"
        if i < attempts - 1:
            time.sleep(1.5)
    raise DemoSeedError(f"No monitored river with a mapped corridor was found, and OpenStreetMap could not be reached right now ({last}). "
                        "Add a river with the search box first, or try again in a minute.")


def _create_river(snap_to_river_center, build_buffer, buffer_radius_m):
    buffer_radius_m = int(buffer_radius_m)      # the column is an INTEGER: Postgres rejects 1000.0 (config holds a float)
    source = _pick_real_river()
    if source:   # copy the real corridor: no network call
        base = str(source["name"]).strip()
        row = {
            "name": base + DEMO_SUFFIX, "state": source.get("state"), "district": source.get("district"),
            "center_lat": source["center_lat"], "center_lng": source["center_lng"],
            "buffer_radius_m": int(source.get("buffer_radius_m") or buffer_radius_m),
            "buffer_geojson": source.get("buffer_geojson") or build_buffer(_points(source), buffer_radius_m),
            "monitor_points": copy.deepcopy(_points(source)),
        }
    else:        # no river yet: map the real channel from OpenStreetMap
        lat, lng, pts = _map_real_channel(snap_to_river_center)
        row = {"name": DEMO_FALLBACK_NAME + DEMO_SUFFIX, "state": "Karnataka", "district": "Davanagere",
               "center_lat": lat, "center_lng": lng, "buffer_radius_m": buffer_radius_m,
               "buffer_geojson": build_buffer(pts, buffer_radius_m), "monitor_points": pts}
    row.update({"scan_interval_days": 5, "monitoring_enabled": False})
    river, dropped = insert_dropping_missing_columns(table_insert, "rivers", row, OPTIONAL_RIVER_COLUMNS)
    river.setdefault("monitor_points", row["monitor_points"])
    return river, dropped


# ── the generated evidence picture ───────────────────────────────────────
def _draw_truck(img, box):
    import cv2
    import numpy as np
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    for cx in (x1 + int(0.20 * w), x1 + int(0.72 * w)):                       # wheels
        cv2.circle(img, (cx, y2 - int(0.10 * h)), int(0.11 * h), (25, 25, 25), -1)
        cv2.circle(img, (cx, y2 - int(0.10 * h)), int(0.05 * h), (150, 150, 150), -1)
    cv2.rectangle(img, (x1 + int(0.30 * w), y1 + int(0.30 * h)), (x2, y2 - int(0.20 * h)), (40, 110, 225), -1)   # dump bed (orange)
    cv2.rectangle(img, (x1 + int(0.30 * w), y1 + int(0.30 * h)), (x2, y2 - int(0.20 * h)), (20, 60, 140), 3)
    heap = [(x1 + int(0.33 * w), y1 + int(0.30 * h)), (x1 + int(0.55 * w), y1 + int(0.06 * h)),
            (x1 + int(0.80 * w), y1 + int(0.10 * h)), (x2 - 6, y1 + int(0.30 * h))]
    cv2.fillPoly(img, [np.array(heap, dtype="int32")], (120, 175, 205))                                       # load of sand
    cv2.rectangle(img, (x1, y1 + int(0.40 * h)), (x1 + int(0.28 * w), y2 - int(0.20 * h)), (70, 70, 75), -1)  # cab
    cv2.rectangle(img, (x1 + 6, y1 + int(0.46 * h)), (x1 + int(0.20 * w), y1 + int(0.64 * h)), (215, 225, 230), -1)  # window


def _draw_excavator(img, box):
    import cv2
    import numpy as np
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    cv2.rectangle(img, (x1 + int(0.02 * w), y2 - int(0.14 * h)), (x1 + int(0.62 * w), y2), (45, 45, 48), -1)   # tracks
    cv2.rectangle(img, (x1 + int(0.06 * w), y1 + int(0.50 * h)), (x1 + int(0.50 * w), y2 - int(0.14 * h)), (30, 200, 250), -1)  # house (yellow)
    cv2.rectangle(img, (x1 + int(0.10 * w), y1 + int(0.56 * h)), (x1 + int(0.26 * w), y1 + int(0.72 * h)), (215, 225, 230), -1)  # cab window
    boom = np.array([(x1 + int(0.44 * w), y1 + int(0.56 * h)), (x1 + int(0.78 * w), y1 + int(0.10 * h)), (x2 - int(0.04 * w), y1 + int(0.42 * h))], dtype="int32")
    cv2.polylines(img, [boom], False, (30, 200, 250), 14)                                                       # boom + stick
    bucket = np.array([(x2 - int(0.10 * w), y1 + int(0.40 * h)), (x2, y1 + int(0.40 * h)), (x2 - int(0.02 * w), y1 + int(0.62 * h)), (x2 - int(0.14 * w), y1 + int(0.60 * h))], dtype="int32")
    cv2.fillPoly(img, [bucket], (50, 50, 55))
    pile = np.array([(x2 - int(0.22 * w), y2 - int(0.02 * h)), (x2 - int(0.04 * w), y2 - int(0.02 * h)), (x2 - int(0.13 * w), y2 - int(0.26 * h))], dtype="int32")
    cv2.fillPoly(img, [pile], (120, 175, 205))                                                                 # sand pile


def _draw_person(img, box, shirt):
    import cv2
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    cx = (x1 + x2) // 2
    cv2.circle(img, (cx, y1 + int(0.10 * h)), max(6, int(0.10 * h)), (150, 175, 210), -1)                       # head
    cv2.rectangle(img, (x1 + int(0.15 * w), y1 + int(0.22 * h)), (x2 - int(0.15 * w), y1 + int(0.60 * h)), shirt, -1)  # torso
    cv2.rectangle(img, (x1 + int(0.20 * w), y1 + int(0.60 * h)), (x2 - int(0.20 * w), y2), (60, 50, 45), -1)    # legs


def render_scene(path):
    """Draws the illustration (river, sandy bank, 2 trucks, an excavator, 3 people), boxes them like the real detector
    does, and stamps it as a TEST illustration. Returns the detection dicts that match the picture."""
    import cv2
    import numpy as np
    from detection import detector
    img = np.zeros((SCENE_H, SCENE_W, 3), np.uint8)
    for y in range(SCENE_H):                                                    # sky -> water -> sand bank
        if y < 150:
            c = (225 - y // 4, 215 - y // 6, 190)
        elif y < 300:
            c = (95, 135 - (y - 150) // 10, 140)
        else:
            c = (115 + (y - 300) // 8, 165 + (y - 300) // 12, 195 + (y - 300) // 10)
        img[y, :] = np.array(c, np.uint8)
    cv2.rectangle(img, (0, 120), (SCENE_W, 150), (60, 95, 70), -1)             # far tree line
    rng = np.random.default_rng(7)
    img = np.clip(img.astype(np.int16) + rng.normal(0, 7, img.shape).astype(np.int16), 0, 255).astype(np.uint8)
    for (cls, _raw, _conf, box) in SCENE_OBJECTS:
        if cls == "truck":
            _draw_truck(img, box)
        elif cls == "jcb":
            _draw_excavator(img, box)
    for i, (_cls, _raw, _conf, box) in enumerate([o for o in SCENE_OBJECTS if o[0] == "person"]):
        _draw_person(img, box, [(60, 60, 200), (200, 120, 40), (60, 160, 60)][i % 3])

    dets = [{"class_name": c, "raw_class": raw, "confidence": conf, "bbox": tuple(float(v) for v in box), "model": "demo",
             "role": "evidence", "view": "original", "track_id": None, "frame_number": 0, "timestamp_s": 0.0}
            for (c, raw, conf, box) in SCENE_OBJECTS]
    img = detector.draw_detections(img, dets)

    overlay = img.copy()                                                         # unmistakable TEST stamp
    for yy in (230, 400, 570):
        cv2.putText(overlay, "TEST - SIMULATED IMAGE", (140, yy), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255), 5)
    img = cv2.addWeighted(overlay, 0.28, img, 0.72, 0)
    cv2.rectangle(img, (0, 0), (SCENE_W, 34), (0, 0, 0), -1)
    cv2.putText(img, "TEST DATA - generated illustration, not a photograph and not a real detection", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 200, 255), 2)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 90]):
        raise IOError(f"could not write {path}")
    return dets


# ── the alert + its proof ────────────────────────────────────────────────
def build_alert(river, point, annotated_ref=None, alert_id=None):
    result = reasoning.score_signals(dict(SIMULATED_SIGNALS))
    text = ("TEST ALERT — simulated for demonstration, NOT a real detection. "
            + reasoning._compose_text(result, "rule_based"))
    signals = {
        **SIMULATED_SIGNALS,
        "demo_test": True,
        "score_breakdown": result["breakdown"],
        "reasons": result["reasons"],
        "caps": result["caps"],
        "hotspot_label": point.get("label"),
        "media": {"annotated": annotated_ref} if annotated_ref else {},
    }
    return {
        "id": alert_id or str(uuid.uuid4()), "river_id": river["id"], "source": "manual_upload",
        "severity": result["severity"], "confidence": result["score"],
        "lat": point["lat"], "lng": point["lng"], "inside_buffer": True,
        "reasoning_text": text, "reasoning_signals": signals,
        "annotated_image_path": annotated_ref,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }


def _create_alert_with_proof(river):
    """Alert row + evidence image + detection rows + hashed PDF. Image/PDF problems become warnings; the alert itself is never lost."""
    import pipeline
    warnings = []
    alert_id = str(uuid.uuid4())
    points = _points(river)
    hotspot = points[len(points) // 2] if points else {"lat": river["center_lat"], "lng": river["center_lng"], "label": "river centre"}

    local_img, dets, ref = None, [], None
    try:
        local_img = Path(config.EVIDENCE_DIR) / f"{alert_id}_annotated.jpg"
        dets = render_scene(local_img)
        ref = gcs_storage.upload_media(alert_id, {"annotated": str(local_img)})["annotated"]   # gs:// ref, or the local path if GCS is off
    except Exception as e:  # noqa: BLE001
        logger.exception("Test data: evidence image failed")
        warnings.append(f"Evidence image could not be generated: {e.__class__.__name__}: {str(e)[:120]}")
        local_img, ref = None, None

    alert = _step("saving the alert", table_insert, "alerts", build_alert(river, hotspot, ref, alert_id))
    if dets:
        try:
            pipeline._insert_detections(alert_id, dets)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Test data: detection rows not stored: {e}")
            warnings.append(f"Detection rows could not be stored: {e.__class__.__name__}")
    pipeline._finalize_evidence(alert, river["name"], dets, [str(local_img)] if local_img else [], alert.get("reasoning_signals") or {})
    warnings += alert.pop("warnings", [])
    if warnings:
        alert["warnings"] = warnings
    return alert


def _demo_alerts(river):
    return [a for a in table_select("alerts", filters={"river_id": river["id"]})
            if (a.get("reasoning_signals") or {}).get("demo_test") is True]


def seed(snap_to_river_center, build_buffer, buffer_radius_m):
    """Creates (or completes) the test river + HIGH alert with proof. Idempotent: calling it twice never duplicates anything."""
    warnings = []
    existing = _step("looking for existing test data", demo_rivers)
    if existing:
        river = existing[0]
    else:
        river, dropped = _step("creating the test river", _create_river, snap_to_river_center, build_buffer, buffer_radius_m)
        if dropped:
            warnings.append(f"Your database has no {' / '.join(dropped)} column on the rivers table, so the river outline is not stored "
                            "(run supabase/migrations/0001_init.sql). The test alert still works.")

    complete = [a for a in _step("reading alerts", _demo_alerts, river) if a.get("evidence_pdf_url") and a.get("annotated_image_path")]
    if complete:
        return {"river": river, "alert": complete[0], "created": False, "warnings": warnings}
    for stale in _demo_alerts(river):    # an earlier, proof-less test alert: replace it (it is test data)
        table_delete_where("detections", {"alert_id": stale["id"]})
        table_delete("alerts", stale["id"])
    alert = _create_alert_with_proof(river)
    return {"river": river, "alert": alert, "created": True, "warnings": warnings + alert.get("warnings", [])}


def remove():
    """Deletes the test river(s) and every alert that belongs to them. Never touches real rivers."""
    rivers = demo_rivers()
    for r in rivers:
        for a in table_select("alerts", filters={"river_id": r["id"]}):
            table_delete_where("detections", {"alert_id": a["id"]})
        table_delete_where("alerts", {"river_id": r["id"]})
        table_delete("rivers", r["id"])
    return {"removed_rivers": len(rivers)}