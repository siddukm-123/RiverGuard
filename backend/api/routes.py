import logging
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

import config
from api.supabase_client import table_select, table_insert
import pipeline

logger = logging.getLogger(__name__)
api_bp = Blueprint("api", __name__, url_prefix="/api")

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp"}
ALLOWED_VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv"}


@api_bp.route("/health", methods=["GET"])
def health():
    """Dedicated healthcheck endpoint — no DB calls, no heavy imports, instant response.
    Used by Railway's healthcheckPath instead of /api/metrics so a slow/misconfigured
    Supabase connection can never fail the deploy healthcheck."""
    return jsonify({"status": "ok"}), 200


@api_bp.route("/rivers", methods=["GET"])
def get_rivers():
    return jsonify(table_select("rivers"))


@api_bp.route("/rivers/search", methods=["GET"])
def search_rivers():
    """Geocodes a free-text query ('Tungabhadra river Davangere') into candidate locations to pick from."""
    from satellite.geocode import search_place
    query = request.args.get("q", "")
    results = search_place(query)
    return jsonify(results)


@api_bp.route("/rivers", methods=["POST"])
def create_river():
    """
    Adds a river to monitor. Snaps the location onto the nearest river channel
    (not the city center), builds corridor monitor points, and stores the river.
    """
    try:
        data = request.get_json(force=True)
        required = ["name", "center_lat", "center_lng"]
        if not all(k in data for k in required):
            return jsonify({"error": f"Missing required fields: {required}"}), 400

        from satellite.river_corridor import snap_to_river_center
        center_lat, center_lng, monitor_points = snap_to_river_center(
            float(data["center_lat"]), float(data["center_lng"]),
            river_name=data.get("name"),
        )
        if not monitor_points:
            return jsonify({
                "error": "No river channel found near this location. Try a more specific river name (e.g. 'Yamuna river Agra')."
            }), 400

        row = {
            "name": data["name"],
            "state": data.get("state"),
            "district": data.get("district"),
            "center_lat": center_lat,
            "center_lng": center_lng,
            "buffer_radius_m": int(data.get("buffer_radius_m", config.DEFAULT_BUFFER_RADIUS_M)),
            "scan_interval_days": int(data.get("scan_interval_days", 5)),
            "monitoring_enabled": bool(data.get("monitoring_enabled", True)),
            "monitor_points": monitor_points,
        }
        try:
            river = table_insert("rivers", row)
        except Exception as e:
            err = str(e).lower()
            if "monitor_points" in err:
                logger.warning("monitor_points column missing — run latest migration SQL.")
                row.pop("monitor_points", None)
                river = table_insert("rivers", row)
                river["monitor_points"] = monitor_points
            else:
                logger.exception("Failed to insert river")
                return jsonify({"error": f"Database error: {e}"}), 500
        return jsonify(river), 201
    except Exception as e:
        logger.exception("create_river failed")
        return jsonify({"error": str(e)}), 500


@api_bp.route("/rivers/<river_id>", methods=["DELETE"])
def delete_river(river_id):
    """Remove a monitored river and its associated satellite alerts."""
    from api.supabase_client import table_delete, table_delete_where
    matches = table_select("rivers", filters={"id": river_id})
    if not matches:
        return jsonify({"error": "River not found"}), 404
    table_delete_where("alerts", {"river_id": river_id})
    table_delete("rivers", river_id)
    return jsonify({"ok": True, "deleted_id": river_id})


@api_bp.route("/rivers/<river_id>/monitoring", methods=["PATCH"])
def update_river_monitoring(river_id):
    """Toggle auto-monitoring on/off, or change the scan interval, for a river."""
    from api.supabase_client import table_update
    data = request.get_json(force=True)
    updates = {}
    if "monitoring_enabled" in data:
        updates["monitoring_enabled"] = bool(data["monitoring_enabled"])
    if "scan_interval_days" in data:
        updates["scan_interval_days"] = int(data["scan_interval_days"])
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    river = table_update("rivers", river_id, updates)
    if not river:
        return jsonify({"error": "River not found"}), 404
    return jsonify(river)


@api_bp.route("/rivers/<river_id>/scan-history", methods=["GET"])
def get_river_scan_history(river_id):
    """All satellite scan alerts for a river with full per-point corridor results."""
    matches = table_select("rivers", filters={"id": river_id})
    if not matches:
        return jsonify({"error": "River not found"}), 404
    river = matches[0]
    alerts = table_select("alerts", filters={"river_id": river_id}, order_by="detected_at", desc=True, limit=50)
    satellite = [a for a in alerts if a.get("source") == "satellite"]
    return jsonify({"river": river, "scans": satellite})


@api_bp.route("/rivers/<river_id>/refresh-corridor", methods=["POST"])
def refresh_river_corridor(river_id):
    """Re-build on-river monitor points (e.g. after algorithm update)."""
    from api.supabase_client import table_update
    from satellite.river_corridor import snap_to_river_center
    matches = table_select("rivers", filters={"id": river_id})
    if not matches:
        return jsonify({"error": "River not found"}), 404
    river = matches[0]
    clat, clng, points = snap_to_river_center(
        river["center_lat"], river["center_lng"], river_name=river.get("name"),
    )
    if not points:
        return jsonify({"error": "Could not find river channel for corridor refresh."}), 400
    updated = table_update("rivers", river_id, {
        "center_lat": clat, "center_lng": clng, "monitor_points": points,
    })
    updated["monitor_points"] = points
    return jsonify(updated)


@api_bp.route("/alerts", methods=["GET"])
def get_alerts():
    severity = request.args.get("severity")
    filters = {"severity": severity} if severity else None
    alerts = table_select("alerts", filters=filters, order_by="detected_at", desc=True, limit=200)
    return jsonify(alerts)


@api_bp.route("/alerts/<alert_id>/detections", methods=["GET"])
def get_alert_detections(alert_id):
    return jsonify(table_select("detections", filters={"alert_id": alert_id}))


@api_bp.route("/alerts/<alert_id>/annotated.jpg", methods=["GET"])
def get_annotated_image(alert_id):
    alerts = table_select("alerts", filters={"id": alert_id})
    if not alerts:
        return jsonify({"error": "Alert not found"}), 404
    # The pipeline saves annotated frames into EVIDENCE_DIR named annotated_<ts>.jpg;
    # we look it up via the alert's stored path if present.
    path = alerts[0].get("annotated_image_path")
    if not path or not Path(path).exists():
        return jsonify({"error": "No annotated image for this alert"}), 404
    return send_file(path, mimetype="image/jpeg")


@api_bp.route("/alerts/<alert_id>/before.jpg", methods=["GET"])
def get_before_image(alert_id):
    alerts = table_select("alerts", filters={"id": alert_id})
    if not alerts or not alerts[0].get("before_image_url"):
        return jsonify({"error": "No before-image for this alert"}), 404
    path = alerts[0]["before_image_url"]
    if not Path(path).exists():
        return jsonify({"error": "Image missing on disk"}), 404
    return send_file(path, mimetype="image/jpeg")


@api_bp.route("/alerts/<alert_id>/after.jpg", methods=["GET"])
def get_after_image(alert_id):
    alerts = table_select("alerts", filters={"id": alert_id})
    if not alerts or not alerts[0].get("after_image_url"):
        return jsonify({"error": "No after-image for this alert"}), 404
    path = alerts[0]["after_image_url"]
    if not Path(path).exists():
        return jsonify({"error": "Image missing on disk"}), 404
    return send_file(path, mimetype="image/jpeg")


@api_bp.route("/alerts/<alert_id>/evidence.pdf", methods=["GET"])
def download_evidence(alert_id):
    alerts = table_select("alerts", filters={"id": alert_id})
    if not alerts or not alerts[0].get("evidence_pdf_url"):
        return jsonify({"error": "Evidence not found"}), 404
    path = alerts[0]["evidence_pdf_url"]
    if not Path(path).exists():
        return jsonify({"error": "Evidence file missing on disk"}), 404
    return send_file(path, as_attachment=True)


@api_bp.route("/metrics", methods=["GET"])
def get_metrics():
    alerts = table_select("alerts")
    rivers = table_select("rivers")
    return jsonify({
        "total_alerts": len(alerts),
        "high_severity": len([a for a in alerts if a["severity"] == "HIGH"]),
        "watch_severity": len([a for a in alerts if a["severity"] == "WATCH"]),
        "rivers_monitored": len(rivers),
        "manual_uploads": len([a for a in alerts if a.get("source") == "manual_upload"]),
        "satellite_scans": len([a for a in alerts if a.get("source") == "satellite"]),
    })


@api_bp.route("/upload", methods=["POST"])
def upload_and_detect():
    """
    Accepts a multipart/form-data POST with:
      - file: the image or video
      - river_id: optional, for zone-check context
      - lat, lng: optional, if the operator knows where this footage is from
    Runs the full detection -> tracking -> clustering -> reasoning pipeline
    synchronously and returns the resulting alert.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    ext = Path(file.filename).suffix.lower()
    is_image = ext in ALLOWED_IMAGE_EXT
    is_video = ext in ALLOWED_VIDEO_EXT
    if not (is_image or is_video):
        return jsonify({"error": f"Unsupported file type '{ext}'. Allowed: images {sorted(ALLOWED_IMAGE_EXT)}, video {sorted(ALLOWED_VIDEO_EXT)}"}), 400

    save_path = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    file.save(save_path)

    river = None
    river_id = request.form.get("river_id")
    if river_id:
        matches = table_select("rivers", filters={"id": river_id})
        river = matches[0] if matches else None

    lat = request.form.get("lat", type=float)
    lng = request.form.get("lng", type=float)
    if lat is None and river:
        lat = river["center_lat"]
    if lng is None and river:
        lng = river["center_lng"]

    upload_row = table_insert("uploads", {
        "original_name": file.filename,
        "file_type": "image" if is_image else "video",
        "storage_path": str(save_path),
        "status": "processing",
    })

    try:
        if is_image:
            alert = pipeline.process_image_upload(save_path, river=river, lat=lat, lng=lng)
        else:
            alert = pipeline.process_video_upload(save_path, river=river, lat=lat, lng=lng)

        from api.supabase_client import table_update
        table_update("uploads", upload_row["id"], {"status": "done", "result_alert_id": alert["id"]})
        return jsonify(alert), 201

    except Exception as e:
        logger.exception("Detection pipeline failed")
        from api.supabase_client import table_update
        table_update("uploads", upload_row["id"], {"status": "failed", "error_message": str(e)})
        return jsonify({"error": f"Detection failed: {e}"}), 500


@api_bp.route("/satellite-scan", methods=["POST"])
def trigger_satellite_scan():
    """
    Manually triggers a satellite before/after scan for one river right now.
    Defaults: after=today, before=5 days ago (Sentinel-2 comparison window).
    """
    from datetime import datetime, timezone, timedelta
    data = request.get_json(force=True)
    river_id = data.get("river_id")
    if not river_id:
        return jsonify({"error": "river_id is required"}), 400

    matches = table_select("rivers", filters={"id": river_id})
    if not matches:
        return jsonify({"error": "River not found"}), 404
    river = matches[0]

    after_date = data.get("after_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    before_date = data.get("before_date") or (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")

    try:
        from api.supabase_client import table_update
        alert = pipeline.process_satellite_scan(river, before_date, after_date)
        table_update("rivers", river["id"], {"last_scanned_at": datetime.now(timezone.utc).isoformat()})
        return jsonify(alert), 201
    except Exception as e:
        logger.exception("Satellite scan failed")
        return jsonify({"error": f"Satellite scan failed: {e}"}), 500
