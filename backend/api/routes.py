import base64
import hmac
import io
import json
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

import config
import demo_data
import gcs_storage
import jobs
import pipeline
from api.supabase_client import (table_select, table_insert, table_update, table_delete,
                                 table_delete_where, storage_mode)

logger = logging.getLogger(__name__)
api_bp = Blueprint("api", __name__, url_prefix="/api")

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp"}
ALLOWED_VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv"}
LOWLIGHT_MODES = ("off", "auto", "force")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")
IMAGE_CACHE_SECONDS = 86400  # media of an alert never changes, so browsers / Vercel may cache it


class ApiError(Exception):
    """An error with an HTTP status + machine-readable code, rendered as consistent JSON."""
    def __init__(self, message, status=400, code="bad_request", details=None):
        super().__init__(message)
        self.http_status, self.code, self.details = status, code, details


def _err(message, status=400, code="bad_request", **extra):
    body = {"error": message, "code": code}
    body.update(extra)
    return jsonify(body), status


@api_bp.errorhandler(ApiError)
def _handle_api_error(e):
    return _err(str(e), e.http_status, e.code, **({"details": e.details} if e.details else {}))


@api_bp.errorhandler(413)
def _too_large(e):
    return _err(f"File too large. Maximum upload is {config.MAX_UPLOAD_MB} MB.", 413, "payload_too_large")


@api_bp.errorhandler(404)
def _nf(e):
    return _err("Not found", 404, "not_found")


@api_bp.errorhandler(405)
def _mna(e):
    return _err("Method not allowed", 405, "method_not_allowed")


@api_bp.errorhandler(Exception)
def _unhandled(e):
    ref = uuid.uuid4().hex[:8]
    logger.exception(f"Unhandled API error [ref {ref}] {request.method} {request.path}")
    # Short, secret-free reason so the page (and you) can see WHAT failed; the full traceback is in the server log under this ref.
    return _err("Internal server error", 500, "internal_error", ref=ref, detail=f"{e.__class__.__name__}: {str(e)[:200]}")


@api_bp.before_request
def _require_key_for_writes():
    """Optional write protection: set RIVERGUARD_API_KEY on the backend to require X-API-Key on non-GET calls."""
    if not config.RIVERGUARD_API_KEY or request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    supplied = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(supplied.encode(), config.RIVERGUARD_API_KEY.encode()):
        return _err("A valid X-API-Key header is required for this action.", 401, "unauthorized")
    return None


# ── helpers ─────────────────────────────────────────────────────────────
def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Request body must be a JSON object.", 400, "invalid_json")
    return data


def _float_in(data, key, lo, hi):
    try:
        v = float(data[key])
    except (KeyError, TypeError, ValueError):
        raise ApiError(f"'{key}' must be a number.", 400, "invalid_field")
    if not (lo <= v <= hi):
        raise ApiError(f"'{key}' must be between {lo} and {hi}.", 400, "invalid_field")
    return v


def _parse_date(value, name):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ApiError(f"'{name}' must be YYYY-MM-DD.", 400, "invalid_date")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ApiError(f"'{name}' is not a real calendar date.", 400, "invalid_date")
    return value


def _signals(alert):
    s = alert.get("reasoning_signals")
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except Exception:
            s = {}
    return s if isinstance(s, dict) else {}


def _media_paths(alert):
    media = dict(_signals(alert).get("media") or {})
    if alert.get("annotated_image_path"):
        media.setdefault("annotated", alert["annotated_image_path"])
    return media


def _public_alert(a):
    """Strip server file-system paths from API output; expose stable API URLs instead."""
    out = dict(a)
    aid = out["id"]
    sig = dict(_signals(out))
    media = _media_paths(out)
    sig.pop("media", None)
    avail = []
    for kind, path in media.items():
        if kind == "original" and Path(str(path)).suffix.lower() not in ALLOWED_IMAGE_EXT:
            continue
        avail.append(kind)
    sig["media_available"] = avail
    out["reasoning_signals"] = sig
    if out.get("evidence_pdf_url"):
        out["evidence_pdf_url"] = f"/api/alerts/{aid}/evidence.pdf"
    if out.get("annotated_image_path"):
        out["annotated_image_path"] = f"/api/alerts/{aid}/annotated.jpg"
    if out.get("before_image_url"):
        out["before_image_url"] = f"/api/alerts/{aid}/before.jpg"
    if out.get("after_image_url"):
        out["after_image_url"] = f"/api/alerts/{aid}/after.jpg"
    return out


def _supabase_key_role():
    key = config.SUPABASE_KEY
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except Exception:
        return None


# ── health / status ─────────────────────────────────────────────────────
@api_bp.route("/health", methods=["GET"])
def health():
    """Instant healthcheck - no DB, no model, no network (safe for Railway's healthcheckPath)."""
    return jsonify({"status": "ok"}), 200


def _library_versions():
    from importlib import metadata
    out = {}
    for pkg in ("torch", "torchvision", "ultralytics", "numpy", "opencv-python-headless", "google-cloud-storage"):
        try:
            out[pkg] = metadata.version(pkg)
        except Exception:  # noqa: BLE001
            out[pkg] = None
    return out


@api_bp.route("/system-status", methods=["GET"])
def system_status():
    """What is real, what is demo, what is unconfigured - so the UI never has to guess."""
    from alerts import notifier
    from detection import detector
    from satellite import gee_client
    mode = storage_mode()
    return jsonify({
        "storage": mode,
        "storage_persistent": mode == "supabase",
        "media_storage": "gcs" if gcs_storage.enabled() else "local_disk (lost on every Railway deploy)",
        "gcs_probe": gcs_storage.probe() if request.args.get("probe") == "1" else None,
        "supabase_key_role": _supabase_key_role() if mode == "supabase" else None,
        "satellite": "real_gee" if gee_client.credentials_present() else "synthetic_demo_only",
        "model": detector.get_model_status(load=False),
        "model_probe": detector.probe() if request.args.get("probe") == "1" else None,
        "libraries": _library_versions(),
        "notifications": notifier.configuration_status(),
        "reasoning_llm_configured": bool(config.ANTHROPIC_API_KEY),
        "write_protection": bool(config.RIVERGUARD_API_KEY),
        "scheduler_enabled": __import__("os").getenv("ENABLE_SCHEDULER", "true").lower() == "true",
        "limits": {"max_upload_mb": config.MAX_UPLOAD_MB, "video_max_seconds": config.VIDEO_MAX_SECONDS},
    })


@api_bp.route("/model-status", methods=["GET"])
def model_status():
    """Detector capabilities. ?load=1 forces the weights to load (slow first time) and reports errors."""
    from detection import detector
    return jsonify(detector.get_model_status(load=request.args.get("load") == "1"))


@api_bp.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    j = jobs.get(job_id)
    if not j:
        return _err("Job not found (it expired, or the server restarted while it was running).", 404, "job_not_found")
    body = {"id": j["id"], "kind": j["kind"], "status": j["status"], "error": j["error"], "progress": j.get("progress", 0.0)}
    if j["status"] == "done":
        body["result"] = j["result"]
    return jsonify(body)


# ── rivers ──────────────────────────────────────────────────────────────
@api_bp.route("/rivers", methods=["GET"])
def get_rivers():
    return jsonify(table_select("rivers"))


@api_bp.route("/rivers/search", methods=["GET"])
def search_rivers():
    from satellite.geocode import search_place
    return jsonify(search_place(request.args.get("q", "")))


def _build_buffer(points, radius):
    from detection import zone_checker
    return zone_checker.buffer_from_monitor_points(points, radius)


@api_bp.route("/rivers", methods=["POST"])
def create_river():
    """Adds a river: snaps to the channel, builds corridor points AND the protected-buffer polygon."""
    data = _json_body()
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 200:
        raise ApiError("'name' is required (max 200 chars).", 400, "invalid_field")
    lat = _float_in(data, "center_lat", -90, 90)
    lng = _float_in(data, "center_lng", -180, 180)

    from satellite.river_corridor import snap_to_river_center
    center_lat, center_lng, monitor_points = snap_to_river_center(lat, lng, river_name=name)
    if not monitor_points:
        raise ApiError("No river channel found near this location. Try a more specific river name (e.g. 'Yamuna river Agra').", 400, "no_channel")

    radius = int(data.get("buffer_radius_m", config.DEFAULT_BUFFER_RADIUS_M))
    interval = int(data.get("scan_interval_days", 5))
    if not (1 <= interval <= 90) or not (50 <= radius <= 20000):
        raise ApiError("scan_interval_days must be 1-90 and buffer_radius_m 50-20000.", 400, "invalid_field")
    row = {
        "name": name, "state": data.get("state"), "district": data.get("district"),
        "center_lat": center_lat, "center_lng": center_lng, "buffer_radius_m": radius,
        "buffer_geojson": _build_buffer(monitor_points, radius),
        "scan_interval_days": interval, "monitoring_enabled": bool(data.get("monitoring_enabled", True)),
        "monitor_points": monitor_points,
    }
    try:
        river = table_insert("rivers", row)
    except Exception as e:
        err = str(e).lower()
        if "monitor_points" in err or "buffer_geojson" in err:
            logger.warning("Optional river column missing - run the latest migration SQL.")
            row.pop("monitor_points", None)
            row.pop("buffer_geojson", None)
            river = table_insert("rivers", row)
            river["monitor_points"] = monitor_points
        else:
            logger.exception("Failed to insert river")
            raise ApiError(f"Database error: {e}", 500, "db_error")
    return jsonify(river), 201


@api_bp.route("/demo/seed", methods=["POST"])
def seed_demo_data():
    """Adds the clearly-labelled TEST river + HIGH alert (see demo_data.py). Idempotent."""
    from satellite.river_corridor import snap_to_river_center
    try:
        out = demo_data.seed(snap_to_river_center, _build_buffer, config.DEFAULT_BUFFER_RADIUS_M)
    except demo_data.DemoSeedError as e:
        raise ApiError(str(e), 503, "demo_unavailable")
    return jsonify(out), (201 if out["created"] else 200)


@api_bp.route("/demo", methods=["DELETE"])
def remove_demo_data():
    """Removes the TEST river(s) and their alerts. Real rivers are never touched."""
    return jsonify(demo_data.remove())


@api_bp.route("/rivers/<river_id>", methods=["DELETE"])
def delete_river(river_id):
    if not table_select("rivers", filters={"id": river_id}):
        raise ApiError("River not found", 404, "not_found")
    table_delete_where("alerts", {"river_id": river_id})
    table_delete("rivers", river_id)
    return jsonify({"ok": True, "deleted_id": river_id})


@api_bp.route("/rivers/<river_id>/monitoring", methods=["PATCH"])
def update_river_monitoring(river_id):
    data = _json_body()
    updates = {}
    if "monitoring_enabled" in data:
        updates["monitoring_enabled"] = bool(data["monitoring_enabled"])
    if "scan_interval_days" in data:
        try:
            days = int(data["scan_interval_days"])
        except (TypeError, ValueError):
            raise ApiError("scan_interval_days must be an integer.", 400, "invalid_field")
        if not 1 <= days <= 90:
            raise ApiError("scan_interval_days must be 1-90.", 400, "invalid_field")
        updates["scan_interval_days"] = days
    if not updates:
        raise ApiError("Nothing to update", 400, "nothing_to_update")
    river = table_update("rivers", river_id, updates)
    if not river:
        raise ApiError("River not found", 404, "not_found")
    return jsonify(river)


@api_bp.route("/rivers/<river_id>/scan-history", methods=["GET"])
def get_river_scan_history(river_id):
    matches = table_select("rivers", filters={"id": river_id})
    if not matches:
        raise ApiError("River not found", 404, "not_found")
    alerts = table_select("alerts", filters={"river_id": river_id}, order_by="detected_at", desc=True, limit=50)
    return jsonify({"river": matches[0], "scans": [_public_alert(a) for a in alerts if a.get("source") == "satellite"]})


@api_bp.route("/rivers/<river_id>/refresh-corridor", methods=["POST"])
def refresh_river_corridor(river_id):
    from satellite.river_corridor import snap_to_river_center
    matches = table_select("rivers", filters={"id": river_id})
    if not matches:
        raise ApiError("River not found", 404, "not_found")
    river = matches[0]
    clat, clng, points = snap_to_river_center(river["center_lat"], river["center_lng"], river_name=river.get("name"))
    if not points:
        raise ApiError("Could not find river channel for corridor refresh.", 400, "no_channel")
    updates = {"center_lat": clat, "center_lng": clng, "monitor_points": points,
               "buffer_geojson": _build_buffer(points, river.get("buffer_radius_m") or config.DEFAULT_BUFFER_RADIUS_M)}
    updated = table_update("rivers", river_id, updates)
    updated["monitor_points"] = points
    return jsonify(updated)


# ── alerts ──────────────────────────────────────────────────────────────
@api_bp.route("/alerts", methods=["GET"])
def get_alerts():
    severity = request.args.get("severity")
    if severity and severity not in ("HIGH", "WATCH", "SAFE"):
        raise ApiError("severity must be HIGH, WATCH or SAFE", 400, "invalid_field")
    alerts = table_select("alerts", filters={"severity": severity} if severity else None,
                          order_by="detected_at", desc=True, limit=200)
    return jsonify([_public_alert(a) for a in alerts])


def _get_alert(alert_id):
    rows = table_select("alerts", filters={"id": alert_id})
    if not rows:
        raise ApiError("Alert not found", 404, "not_found")
    return rows[0]


@api_bp.route("/alerts/<alert_id>", methods=["GET"])
def get_alert(alert_id):
    return jsonify(_public_alert(_get_alert(alert_id)))


@api_bp.route("/alerts/<alert_id>/detections", methods=["GET"])
def get_alert_detections(alert_id):
    return jsonify(table_select("detections", filters={"alert_id": alert_id}))


def _send_bytes(data, ctype):
    return send_file(io.BytesIO(data), mimetype=ctype, max_age=IMAGE_CACHE_SECONDS)


def _send_image(path, what):
    """path is either a gs:// reference (bucket) or a local file path (legacy / GCS disabled)."""
    if gcs_storage.is_ref(path):
        got = gcs_storage.download_ref(path)
        if not got:
            raise ApiError(f"{what} is not available (object missing in the storage bucket).", 404, "file_missing")
        return _send_bytes(*got)
    if not path or not Path(path).exists():
        raise ApiError(f"{what} is not available (file missing - set GCS_BUCKET to keep images across Railway deploys).", 404, "file_missing")
    return send_file(path, mimetype="image/jpeg", max_age=IMAGE_CACHE_SECONDS)


def _gcs_fast_path(alert_id, kind):
    """
    Bucket objects have deterministic names, so the image can be fetched WITHOUT a database lookup.
    (The old code did one Supabase query per <img>, which flooded the logs and dropped HTTP/2 connections.)
    Returns a response, or None to fall back to the database path.
    """
    if not gcs_storage.enabled() or not UUID_RE.match(alert_id):
        return None
    got = gcs_storage.download_name(gcs_storage.blob_name(alert_id, kind))
    return _send_bytes(*got) if got else None


@api_bp.route("/alerts/<alert_id>/annotated.jpg", methods=["GET"])
def get_annotated_image(alert_id):
    fast = _gcs_fast_path(alert_id, "annotated")
    if fast:
        return fast
    return _send_image(_media_paths(_get_alert(alert_id)).get("annotated"), "Annotated image")


@api_bp.route("/alerts/<alert_id>/image/<kind>", methods=["GET"])
def get_alert_image(alert_id, kind):
    if kind not in ("original", "annotated", "enhanced", "comparison"):
        raise ApiError("kind must be original, annotated, enhanced or comparison", 400, "invalid_field")
    fast = _gcs_fast_path(alert_id, kind) if kind != "original" else None  # 'original' keeps its extension -> DB lookup
    if fast:
        return fast
    path = _media_paths(_get_alert(alert_id)).get(kind)
    if kind == "original" and path and Path(str(path)).suffix.lower() not in ALLOWED_IMAGE_EXT:
        raise ApiError("The original upload is a video, not an image.", 404, "not_an_image")
    return _send_image(path, f"{kind} image")


@api_bp.route("/alerts/<alert_id>/before.jpg", methods=["GET"])
def get_before_image(alert_id):
    fast = _gcs_fast_path(alert_id, "before")
    if fast:
        return fast
    return _send_image(_get_alert(alert_id).get("before_image_url"), "Before image")


@api_bp.route("/alerts/<alert_id>/after.jpg", methods=["GET"])
def get_after_image(alert_id):
    fast = _gcs_fast_path(alert_id, "after")
    if fast:
        return fast
    return _send_image(_get_alert(alert_id).get("after_image_url"), "After image")


@api_bp.route("/alerts/<alert_id>/evidence.pdf", methods=["GET"])
def download_evidence(alert_id):
    a = _get_alert(alert_id)
    path = a.get("evidence_pdf_url")
    name = f"riverguard_evidence_{alert_id}.pdf"
    if gcs_storage.is_ref(path):
        got = gcs_storage.download_ref(path)
        if not got:
            raise ApiError("Evidence PDF is missing from the storage bucket.", 404, "file_missing")
        return send_file(io.BytesIO(got[0]), as_attachment=True, mimetype="application/pdf", download_name=name)
    if not path or not Path(path).exists():
        raise ApiError("Evidence file missing on disk (set GCS_BUCKET to keep files across Railway deploys).", 404, "file_missing")
    return send_file(path, as_attachment=True, mimetype="application/pdf", download_name=name)


@api_bp.route("/alerts/<alert_id>/evidence/verify", methods=["GET"])
def verify_evidence(alert_id):
    """Recompute SHA-256 over the stored PDF bytes and compare with the digest saved at generation time."""
    import hashlib
    a = _get_alert(alert_id)
    path, stored = a.get("evidence_pdf_url"), a.get("evidence_sha256")
    if gcs_storage.is_ref(path):
        got = gcs_storage.download_ref(path)
        if not got:
            raise ApiError("Evidence file missing from the storage bucket.", 404, "file_missing")
        raw = got[0]
    elif path and Path(path).exists():
        raw = Path(path).read_bytes()
    else:
        raise ApiError("Evidence file missing on disk.", 404, "file_missing")
    actual = hashlib.sha256(raw).hexdigest()
    return jsonify({"alert_id": alert_id, "stored_sha256": stored, "recomputed_sha256": actual,
                    "match": bool(stored) and stored == actual,
                    "meaning": "A match means the file is byte-identical to what was generated. It does not prove the underlying observation is authentic."})


@api_bp.route("/metrics", methods=["GET"])
def get_metrics():
    alerts = table_select("alerts")
    rivers = table_select("rivers")
    sev = lambda s: len([a for a in alerts if a.get("severity") == s])
    return jsonify({
        "total_alerts": len(alerts), "high_severity": sev("HIGH"), "watch_severity": sev("WATCH"),
        "rivers_monitored": len(rivers),
        "rivers_scanned": len([r for r in rivers if r.get("last_scanned_at")]),  # rivers with at least one completed satellite scan
        "manual_uploads": len([a for a in alerts if a.get("source") == "manual_upload"]),
        "satellite_scans": len([a for a in alerts if a.get("source") == "satellite"]),
        "storage": storage_mode(),
    })


# ── upload ──────────────────────────────────────────────────────────────
def _looks_like(path, ext):
    """Cheap magic-byte sniff so a renamed .exe/.html is rejected before any decoder touches it."""
    with open(path, "rb") as f:
        head = f.read(16)
    if ext in (".jpg", ".jpeg"):
        return head[:3] == b"\xff\xd8\xff"
    if ext == ".png":
        return head[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == ".bmp":
        return head[:2] == b"BM"
    if ext in (".mp4", ".mov"):
        return head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip")
    if ext == ".avi":
        return head[:4] == b"RIFF" and head[8:12] == b"AVI "
    if ext == ".mkv":
        return head[:4] == b"\x1a\x45\xdf\xa3"
    return False


def _track_upload(upload_id, fields):
    """Updates the 'uploads' log row. Bookkeeping only: a failure here is logged but must never fail or mask the detection."""
    if not upload_id:
        return
    try:
        table_update("uploads", upload_id, fields)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not update uploads row {upload_id}: {e.__class__.__name__}: {e}")


def _run_upload(save_path, is_image, river, lat, lng, lowlight_mode, original_name, upload_id):
    """Executed inline or in a job. Raises ApiError with the right status for bad media / missing model."""
    from detection.detector import ModelUnavailableError
    try:
        if is_image:
            alert = pipeline.process_image_upload(save_path, river=river, lat=lat, lng=lng, lowlight_mode=lowlight_mode, original_name=original_name)
        else:
            alert = pipeline.process_video_upload(save_path, river=river, lat=lat, lng=lng, lowlight_mode=lowlight_mode, original_name=original_name)
    except ValueError as e:
        _track_upload(upload_id, {"status": "failed", "error_message": str(e)[:500]})
        raise ApiError(str(e), 422, "invalid_media")
    except ModelUnavailableError as e:
        _track_upload(upload_id, {"status": "failed", "error_message": str(e)[:500]})
        raise ApiError(f"Detection model is unavailable: {e}", 503, "model_unavailable")
    except Exception as e:
        logger.exception("Detection pipeline failed")
        _track_upload(upload_id, {"status": "failed", "error_message": str(e)[:500]})
        raise ApiError(f"Detection failed: {e.__class__.__name__}: {str(e)[:300]}", 500, "pipeline_error")
    _track_upload(upload_id, {"status": "done", "result_alert_id": alert["id"]})
    return _public_alert(alert) | {k: alert[k] for k in ("detections", "object_counts", "analysis", "video_overlay") if k in alert} | (
        {"track_behaviors": alert["track_behaviors"], "detections_total": alert["detections_total"]} if "track_behaviors" in alert else {})


@api_bp.route("/upload", methods=["POST"])
def upload_and_detect():
    """
    multipart/form-data: file, river_id?, lat?, lng?, lowlight (off|auto|force)?, async (1)?
    Sync (default): returns the alert (201). async=1: returns {job_id} (202); poll /api/jobs/<id>.
    """
    if "file" not in request.files:
        raise ApiError("No file uploaded", 400, "no_file")
    file = request.files["file"]
    if not file.filename:
        raise ApiError("Empty filename", 400, "no_file")
    ext = Path(file.filename).suffix.lower()
    is_image, is_video = ext in ALLOWED_IMAGE_EXT, ext in ALLOWED_VIDEO_EXT
    if not (is_image or is_video):
        raise ApiError(f"Unsupported file type '{ext}'. Allowed: images {sorted(ALLOWED_IMAGE_EXT)}, video {sorted(ALLOWED_VIDEO_EXT)}", 400, "unsupported_type")

    lowlight_mode = (request.form.get("lowlight") or "off").lower()
    if lowlight_mode not in LOWLIGHT_MODES:
        raise ApiError(f"lowlight must be one of {list(LOWLIGHT_MODES)}", 400, "invalid_field")

    lat, lng = request.form.get("lat", type=float), request.form.get("lng", type=float)
    if (lat is None) != (lng is None):
        raise ApiError("Provide both lat and lng, or neither.", 400, "invalid_field")
    if lat is not None and not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ApiError("lat/lng out of range.", 400, "invalid_field")

    river = None
    river_id = request.form.get("river_id")
    if river_id:
        matches = table_select("rivers", filters={"id": river_id})
        if not matches:
            raise ApiError("Selected river not found.", 404, "not_found")
        river = matches[0]

    save_path = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    file.save(save_path)
    if save_path.stat().st_size == 0 or not _looks_like(save_path, ext):
        save_path.unlink(missing_ok=True)
        raise ApiError(f"The file content does not look like a valid {ext} file.", 422, "invalid_media")

    try:
        upload_row = table_insert("uploads", {"original_name": file.filename[:255], "file_type": "image" if is_image else "video",
                                              "storage_path": str(save_path), "status": "processing"})
        upload_id = upload_row["id"]
    except Exception as e:  # noqa: BLE001 - the log row is optional; detection still runs
        logger.warning(f"Could not create uploads row: {e.__class__.__name__}: {e}")
        upload_id = None
    args = (save_path, is_image, river, lat, lng, lowlight_mode, file.filename, upload_id)
    if request.form.get("async") in ("1", "true"):
        return jsonify({"job_id": jobs.submit("upload", _run_upload, *args), "status": "queued"}), 202
    return jsonify(_run_upload(*args)), 201


# ── satellite scan ──────────────────────────────────────────────────────
def _do_scan(river, before_date, after_date):
    """Returns (payload, http_status). No valid comparison -> payload status 'no_data' and NO alert."""
    try:
        alert = pipeline.process_satellite_scan(river, before_date, after_date)
    except pipeline.NoSatelliteData as e:
        return {"status": "no_data", "message": str(e), "details": e.details}, 200
    table_update("rivers", river["id"], {"last_scanned_at": datetime.now(timezone.utc).isoformat()})
    return _public_alert(alert) | {"status": "ok", "analysis": alert.get("analysis")}, 201


def _scan_job(river, before_date, after_date):
    payload, _ = _do_scan(river, before_date, after_date)
    return payload


@api_bp.route("/satellite-scan", methods=["POST"])
def trigger_satellite_scan():
    """
    Body: {river_id, before_date?, after_date?, async?}. Defaults: after = today; before = the clean scene
    ~SAT_BASELINE_DAYS earlier than the chosen 'after' scene. Scenes must be distinct and well separated.
    """
    data = _json_body()
    river_id = data.get("river_id")
    if not river_id:
        raise ApiError("river_id is required", 400, "invalid_field")
    matches = table_select("rivers", filters={"id": river_id})
    if not matches:
        raise ApiError("River not found", 404, "not_found")
    river = matches[0]

    after_date = _parse_date(data.get("after_date"), "after_date")
    before_date = _parse_date(data.get("before_date"), "before_date")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if after_date and after_date > today:
        raise ApiError("after_date cannot be in the future.", 400, "invalid_date")
    if before_date and (after_date or today) <= before_date:
        raise ApiError("before_date must be earlier than after_date.", 400, "invalid_date")
    after_date = after_date or today

    if data.get("async"):
        return jsonify({"job_id": jobs.submit("satellite-scan", _scan_job, river, before_date, after_date), "status": "queued"}), 202
    try:
        payload, status = _do_scan(river, before_date, after_date)
    except Exception as e:
        logger.exception("Satellite scan failed")
        raise ApiError(f"Satellite scan failed: {e}", 500, "scan_error")
    return jsonify(payload), status