"""
Test (demo) data: one river with a HIGH-severity alert, so the dashboard can be shown with every state.

Honesty rules (so test data can never be mistaken for a real incident):
  * the river name always ends with " (Test)"  -> the dashboard, feed and map popups show a TEST badge;
  * the alert is marked reasoning_signals.demo_test = true and its text starts with "TEST ALERT";
  * the river corridor is the REAL channel of the real river, mapped from OpenStreetMap - no invented geometry
    (if OpenStreetMap is unreachable, seeding fails with a clear message instead of guessing coordinates);
  * the risk score is computed by the real rule engine (detection.reasoning) from the simulated inputs below,
    not typed in by hand;
  * the river is created with monitoring OFF and the scheduler never scans it automatically;
  * everything is removed again with remove().
"""
import logging
import time
import uuid
from datetime import datetime, timezone

from api.supabase_client import table_delete, table_delete_where, table_insert, table_select
from detection import reasoning

logger = logging.getLogger(__name__)

DEMO_SUFFIX = " (Test)"
DEMO_RIVER_NAME = "Tungabhadra River" + DEMO_SUFFIX
DEMO_STATE, DEMO_DISTRICT = "Karnataka", "Davanagere"
# Anchor on the real river near Harihar. The corridor points are snapped to the real channel from OpenStreetMap.
DEMO_ANCHOR = (14.5126, 75.8050)
DEMO_SEARCH_NAME = "Tungabhadra River"

# What the simulated scene "contains": a truck + an excavator + people working together, inside the buffer,
# with a truck loitering (loading). The REAL scorer turns this into the risk score / explanation.
SIMULATED_SIGNALS = {
    "input_type": "image",
    "inside_buffer": True,
    "location_source": "demo",
    "object_counts": {"truck": 2, "jcb": 1, "person": 3},
    "best_cluster": {"has_truck": True, "has_jcb": True, "has_person": True, "count": 6},
    "track_behaviors": [{"class_name": "truck", "behavior": "loitering"}],
    "model_info": {"excavator_detection_available": True},
}


class DemoSeedError(RuntimeError):
    """Raised when the test data cannot be created (nothing is left half-created)."""


def is_demo_river(river):
    return str((river or {}).get("name", "")).endswith(DEMO_SUFFIX)


def demo_rivers():
    return [r for r in table_select("rivers") if is_demo_river(r)]


def _map_real_channel(snap_to_river_center, attempts=3):
    last = None
    for i in range(attempts):
        try:
            lat, lng, points = snap_to_river_center(DEMO_ANCHOR[0], DEMO_ANCHOR[1], river_name=DEMO_SEARCH_NAME)
            if points:
                return lat, lng, points
            last = "no river channel found near the anchor point"
        except Exception as e:  # noqa: BLE001 - Overpass is flaky; retry, then explain
            last = f"{e.__class__.__name__}: {str(e)[:120]}"
        time.sleep(1.5 * (i + 1) if i < attempts - 1 else 0)
    raise DemoSeedError(f"Could not map the real river course from OpenStreetMap right now ({last}). Please try again in a minute.")


def build_alert(river, point):
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
    }
    return {
        "id": str(uuid.uuid4()), "river_id": river["id"], "source": "manual_upload",
        "severity": result["severity"], "confidence": result["score"],
        "lat": point["lat"], "lng": point["lng"], "inside_buffer": True,
        "reasoning_text": text, "reasoning_signals": signals,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }


def seed(snap_to_river_center, build_buffer, buffer_radius_m):
    """Creates (or completes) the test river + HIGH alert. Idempotent: calling it twice never duplicates anything."""
    existing = demo_rivers()
    if existing:
        river = existing[0]
    else:
        center_lat, center_lng, points = _map_real_channel(snap_to_river_center)
        river = table_insert("rivers", {
            "name": DEMO_RIVER_NAME, "state": DEMO_STATE, "district": DEMO_DISTRICT,
            "center_lat": center_lat, "center_lng": center_lng, "buffer_radius_m": buffer_radius_m,
            "buffer_geojson": build_buffer(points, buffer_radius_m), "scan_interval_days": 5,
            "monitoring_enabled": False, "monitor_points": points,
        })
        river.setdefault("monitor_points", points)

    has_alert = [a for a in table_select("alerts", filters={"river_id": river["id"]})
                 if (a.get("reasoning_signals") or {}).get("demo_test") is True]
    if has_alert:
        return {"river": river, "alert": has_alert[0], "created": False}

    points = river.get("monitor_points") or []
    hotspot = points[len(points) // 2] if points else {"lat": river["center_lat"], "lng": river["center_lng"], "label": "river centre"}
    alert = table_insert("alerts", build_alert(river, hotspot))
    return {"river": river, "alert": alert, "created": True}


def remove():
    """Deletes the test river(s) and every alert that belongs to them. Never touches real rivers."""
    rivers = demo_rivers()
    for r in rivers:
        table_delete_where("alerts", {"river_id": r["id"]})
        table_delete("rivers", r["id"])
    return {"removed_rivers": len(rivers)}
