"""
The pipeline that turns a raw input (uploaded image/video, or a scheduled
satellite scan) into a stored alert with reasoning, evidence PDF, and
(for HIGH/WATCH) an outbound notification.
"""
import logging
from datetime import datetime, timezone

import config
from detection import detector, zone_checker, cluster_engine, reasoning
from alerts import pdf_generator, notifier
from api.supabase_client import table_insert, table_select

logger = logging.getLogger(__name__)


def _resolve_zone(lat, lng, river):
    """Checks whether (lat, lng) falls inside the given river's buffer. Returns True/False/None."""
    if lat is None or lng is None or river is None:
        return None
    if river.get("buffer_geojson"):
        return zone_checker.point_in_buffer(lat, lng, river["buffer_geojson"])
    return zone_checker.simple_distance_check(
        lat, lng, river["center_lat"], river["center_lng"],
        radius_m=river.get("buffer_radius_m") or config.DEFAULT_BUFFER_RADIUS_M,
    )


def _notify_if_needed(alert, river):
    if alert["severity"] not in ("HIGH", "WATCH") or river is None:
        return
    officers = table_select("officers", filters={"river_id": river["id"]})
    for officer in officers:
        statuses = notifier.notify_officer(alert, river["name"], officer)
        if statuses:
            from api.supabase_client import table_update
            table_update("alerts", alert["id"], statuses)


def process_image_upload(image_path, river=None, lat=None, lng=None):
    """
    Runs the full pipeline on a single uploaded image.
    Returns the created alert dict.
    """
    detections, frame = detector.detect_image_file(image_path)
    detections = cluster_engine.cluster_pixels(detections)
    cluster_summaries = cluster_engine.summarize_clusters(detections)
    best_cluster = max(cluster_summaries, key=lambda c: c["count"], default=None)

    inside_buffer = _resolve_zone(lat, lng, river)

    signals = {
        "inside_buffer": inside_buffer,
        "best_cluster": best_cluster,
        "track_behaviors": [],
        "satellite_change_score": None,
        "input_type": "image",
    }
    severity, confidence, reasoning_text = reasoning.evaluate(signals)

    annotated = detector.draw_detections(frame, detections)
    import cv2
    annotated_path = config.EVIDENCE_DIR / f"annotated_{datetime.now(timezone.utc).timestamp():.0f}.jpg"
    cv2.imwrite(str(annotated_path), annotated)

    alert = table_insert("alerts", {
        "river_id": river["id"] if river else None,
        "source": "manual_upload",
        "severity": severity,
        "confidence": confidence,
        "lat": lat,
        "lng": lng,
        "inside_buffer": bool(inside_buffer) if inside_buffer is not None else False,
        "reasoning_text": reasoning_text,
        "reasoning_signals": signals,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    })

    for d in detections:
        table_insert("detections", {
            "alert_id": alert["id"],
            "class_name": d["class_name"],
            "confidence": d["confidence"],
            "bbox_x_min": int(d["bbox"][0]), "bbox_y_min": int(d["bbox"][1]),
            "bbox_x_max": int(d["bbox"][2]), "bbox_y_max": int(d["bbox"][3]),
            "track_id": d.get("track_id"),
        })

    pdf_path, sha256 = pdf_generator.generate_evidence_pdf(
        alert, river["name"] if river else "Unknown", detections, [annotated_path]
    )
    from api.supabase_client import table_update
    table_update("alerts", alert["id"], {"evidence_pdf_url": pdf_path, "evidence_sha256": sha256, "annotated_image_path": str(annotated_path)})
    alert["evidence_pdf_url"] = pdf_path
    alert["annotated_image_path"] = str(annotated_path)
    alert["detections"] = detections

    _notify_if_needed(alert, river)
    return alert


def process_video_upload(video_path, river=None, lat=None, lng=None):
    """
    Runs the full pipeline on an uploaded video: detection + tracking across
    sampled frames, then clustering + reasoning on the aggregate behavior.
    Returns the created alert dict.
    """
    detections, track_behaviors, fps, thumbnail = detector.detect_video_file(video_path)
    detections = cluster_engine.cluster_pixels(detections)
    cluster_summaries = cluster_engine.summarize_clusters(detections)
    best_cluster = max(cluster_summaries, key=lambda c: c["count"], default=None)

    inside_buffer = _resolve_zone(lat, lng, river)

    signals = {
        "inside_buffer": inside_buffer,
        "best_cluster": best_cluster,
        "track_behaviors": track_behaviors,
        "satellite_change_score": None,
        "input_type": "video",
        "num_tracked_objects": len(track_behaviors),
    }
    severity, confidence, reasoning_text = reasoning.evaluate(signals)

    annotated_path = None
    if thumbnail is not None:
        frame_dets = [d for d in detections if d.get("frame_number") is not None]
        annotated = detector.draw_detections(thumbnail, frame_dets[:30])
        import cv2
        annotated_path = config.EVIDENCE_DIR / f"annotated_{datetime.now(timezone.utc).timestamp():.0f}.jpg"
        cv2.imwrite(str(annotated_path), annotated)

    alert = table_insert("alerts", {
        "river_id": river["id"] if river else None,
        "source": "manual_upload",
        "severity": severity,
        "confidence": confidence,
        "lat": lat,
        "lng": lng,
        "inside_buffer": bool(inside_buffer) if inside_buffer is not None else False,
        "reasoning_text": reasoning_text,
        "reasoning_signals": signals,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    })

    for d in detections:
        table_insert("detections", {
            "alert_id": alert["id"],
            "class_name": d["class_name"],
            "confidence": d["confidence"],
            "bbox_x_min": int(d["bbox"][0]), "bbox_y_min": int(d["bbox"][1]),
            "bbox_x_max": int(d["bbox"][2]), "bbox_y_max": int(d["bbox"][3]),
            "frame_number": d.get("frame_number"),
            "track_id": d.get("track_id"),
        })

    image_paths = [annotated_path] if annotated_path else None
    pdf_path, sha256 = pdf_generator.generate_evidence_pdf(
        alert, river["name"] if river else "Unknown", detections, image_paths
    )
    from api.supabase_client import table_update
    table_update("alerts", alert["id"], {"evidence_pdf_url": pdf_path, "evidence_sha256": sha256, "annotated_image_path": str(annotated_path) if annotated_path else None})
    alert["evidence_pdf_url"] = pdf_path
    alert["annotated_image_path"] = str(annotated_path) if annotated_path else None
    alert["detections"] = detections
    alert["track_behaviors"] = track_behaviors

    _notify_if_needed(alert, river)
    return alert


def _monitor_points_for_river(river):
    """Returns corridor sample points for a river, computing them on the fly if missing."""
    stored = river.get("monitor_points")
    if stored and isinstance(stored, list) and len(stored) > 0:
        return stored
    from satellite.river_corridor import fetch_monitor_points
    return fetch_monitor_points(
        river["center_lat"], river["center_lng"], river_name=river.get("name"),
    )


def process_satellite_scan(river, before_date, after_date):
    """
    Runs the satellite-tier pipeline for one river: scans multiple points along
    the river corridor (not just the center), compares real Sentinel-2 before/
    after imagery via GEE when configured, and raises an alert at the worst hotspot.
    """
    from satellite import gee_client

    points = _monitor_points_for_river(river)
    # Scan every on-river point (capped for GEE API time)
    points = [p for p in points if p.get("on_river", True)][:10]
    if not points:
        from satellite.river_corridor import fetch_monitor_points
        points = fetch_monitor_points(
            river["center_lat"], river["center_lng"], river_name=river.get("name"),
        )[:10]

    scan_results = []
    for pt in points:
        result = gee_client.get_before_after_pair(pt["lat"], pt["lng"], before_date, after_date)
        scan_results.append({
            "lat": pt["lat"],
            "lng": pt["lng"],
            "label": pt.get("label", "point"),
            "on_river": pt.get("on_river", True),
            **result,
        })

    # Hotspot = highest sandbar/riverbed change score along the corridor
    hotspot = max(scan_results, key=lambda r: r.get("change_score") or 0)
    result = hotspot

    signals = {
        "inside_buffer": True,
        "best_cluster": None,
        "track_behaviors": [],
        "satellite_change_score": result["change_score"],
        "raw_ndwi_diff": result.get("raw_ndwi_diff"),
        "input_type": "satellite",
        "synthetic_data": result.get("synthetic", False),
        "before_date": before_date,
        "after_date": after_date,
        "corridor_points_scanned": len(scan_results),
        "hotspot_label": result.get("label"),
        "scan_results": [
            {
                "lat": r["lat"], "lng": r["lng"], "label": r.get("label"),
                "change_score": r.get("change_score"),
                "raw_ndwi_diff": r.get("raw_ndwi_diff"),
                "synthetic": r.get("synthetic"),
                "on_river": r.get("on_river", True),
            }
            for r in scan_results
        ],
    }
    severity, confidence, reasoning_text = reasoning.evaluate(signals)

    if signals.get("corridor_points_scanned", 1) > 1:
        reasoning_text += (
            f" Scanned {signals['corridor_points_scanned']} on-river bed points "
            f"(before {before_date}, after {after_date}); highest change at {result.get('label', 'hotspot')}."
        )
    if signals.get("synthetic_data"):
        reasoning_text += " Note: satellite imagery unavailable - using demo change score until Google Earth Engine finds cloud-free scenes."
    else:
        reasoning_text += " Based on real Sentinel-2 before/after NDWI comparison."

    # Download real before/after imagery locally if GEE returned thumbnail URLs
    before_local, after_local = None, None
    if result.get("before_thumb_url"):
        ts = datetime.now(timezone.utc).timestamp()
        before_local = config.EVIDENCE_DIR / f"before_{ts:.0f}.jpg"
        after_local = config.EVIDENCE_DIR / f"after_{ts:.0f}.jpg"
        ok1 = gee_client.download_thumbnail(result["before_thumb_url"], before_local)
        ok2 = gee_client.download_thumbnail(result["after_thumb_url"], after_local)
        if not (ok1 and ok2):
            before_local, after_local = None, None

    alert = table_insert("alerts", {
        "river_id": river["id"],
        "source": "satellite",
        "severity": severity,
        "confidence": confidence,
        "lat": result["lat"],
        "lng": result["lng"],
        "inside_buffer": True,
        "reasoning_text": reasoning_text,
        "reasoning_signals": signals,
        "before_image_url": str(before_local) if before_local else None,
        "after_image_url": str(after_local) if after_local else None,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    })

    image_paths = [p for p in (before_local, after_local) if p]
    pdf_path, sha256 = pdf_generator.generate_evidence_pdf(alert, river["name"], [], image_paths or None)
    from api.supabase_client import table_update
    table_update("alerts", alert["id"], {"evidence_pdf_url": pdf_path, "evidence_sha256": sha256})
    alert["evidence_pdf_url"] = pdf_path

    _notify_if_needed(alert, river)
    return alert
