"""
The pipeline that turns a raw input (uploaded image/video, or a scheduled satellite scan) into a
stored alert with reasoning, evidence PDF and (for HIGH/WATCH) an outbound notification.

Stages: validate -> preprocess (optional low-light) -> detect/satellite -> track -> cluster ->
zone check -> reasoning -> alert row -> detections -> evidence PDF (+SHA-256) -> notify.

Everything that is not a first-class DB column (provenance, model info, low-light summary, media
paths, scene ids, score breakdown) is stored inside `alerts.reasoning_signals` (jsonb), so NO
schema migration is required for these features.
"""
import hashlib
import logging
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config
import gcs_storage
from detection import detector, zone_checker, cluster_engine, reasoning
from alerts import pdf_generator, notifier
from api.supabase_client import table_insert, table_insert_many, table_select, table_update

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"SAFE": 0, "WATCH": 1, "HIGH": 2}


class NoSatelliteData(Exception):
    """No valid before/after comparison could be made. Carries operator-facing detail; no alert is created."""
    http_status = 200

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or []


# ── helpers ─────────────────────────────────────────────────────────────
def _now():
    return datetime.now(timezone.utc)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_zone(lat, lng, river, location_source):
    """
    Returns (inside_buffer True/False/None, method). A location that is merely the river's registered
    centre (nobody supplied GPS for the footage) is NOT a measured position, so the zone is 'unknown'.
    """
    if lat is None or lng is None or river is None or location_source != "operator_supplied":
        return None, "unknown (no operator-supplied GPS for this footage)"
    if river.get("buffer_geojson"):
        r = zone_checker.point_in_buffer(lat, lng, river["buffer_geojson"])
        if r is not None:
            return r, "buffer polygon (UTM-correct) point-in-polygon"
    if river.get("monitor_points"):
        r = zone_checker.polyline_distance_check(lat, lng, river["monitor_points"],
                                                 river.get("buffer_radius_m") or config.DEFAULT_BUFFER_RADIUS_M)
        if r is not None:
            return r, "distance to corridor poly-line"
    return (zone_checker.simple_distance_check(lat, lng, river["center_lat"], river["center_lng"],
                                               river.get("buffer_radius_m") or config.DEFAULT_BUFFER_RADIUS_M),
            "distance to river centre point (legacy, coarse)")


def _locate(river, lat, lng):
    """(lat, lng, source). Operator GPS wins; otherwise the river centre is used for MAP DISPLAY only."""
    if lat is not None and lng is not None:
        return lat, lng, "operator_supplied"
    if river:
        return river["center_lat"], river["center_lng"], "river_default"
    return None, None, None


def _save_jpg(path, img):
    import cv2
    ok = cv2.imwrite(str(path), img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        raise IOError(f"Could not write {path}")
    return str(path)


def _comparison_image(original_annotated, enhanced_annotated):
    import cv2
    import numpy as np
    h = min(original_annotated.shape[0], enhanced_annotated.shape[0], 720)

    def fit(img):
        s = h / img.shape[0]
        return cv2.resize(img, (max(1, int(img.shape[1] * s)), h), interpolation=cv2.INTER_AREA)

    a, b = fit(original_annotated), fit(enhanced_annotated)
    for img, text in ((a, "ORIGINAL"), (b, "ENHANCED (contrast only - not night vision)")):
        cv2.rectangle(img, (0, 0), (img.shape[1], 30), (0, 0, 0), -1)
        cv2.putText(img, text, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return np.hstack([a, b])


def _best_cluster(summaries):
    return max(summaries, key=lambda c: (c["has_jcb"], c["count"], c["avg_confidence"]), default=None)


def _insert_detections(alert_id, detections):
    """Batch insert. Tries the optional provenance columns first and degrades to the base schema."""
    if not detections:
        return
    base, ext = [], []
    for d in detections:
        row = {
            "alert_id": alert_id, "class_name": d["class_name"], "confidence": d["confidence"],
            "bbox_x_min": int(d["bbox"][0]), "bbox_y_min": int(d["bbox"][1]),
            "bbox_x_max": int(d["bbox"][2]), "bbox_y_max": int(d["bbox"][3]),
            "frame_number": d.get("frame_number"), "track_id": d.get("track_id"),
        }
        base.append(row)
        ext.append({**row, "timestamp_s": d.get("timestamp_s"), "view": d.get("view"),
                    "model": d.get("model"), "role": d.get("role"), "raw_class": d.get("raw_class")})
    try:
        table_insert_many("detections", ext)
    except Exception as e:
        logger.info(f"Extended detection columns unavailable ({str(e)[:80]}); using base columns. Run 0002 migration to store provenance per row.")
        table_insert_many("detections", base)


def _notify_if_needed(alert, river, signals):
    """
    Notify mapped officers for HIGH/WATCH - but never for synthetic data, never twice for the same
    evidence, and not repeatedly inside NOTIFY_COOLDOWN_HOURS for the same river+source.
    Returns a short string describing what happened (stored in signals by the caller if wanted).
    """
    if alert["severity"] not in ("HIGH", "WATCH") or river is None:
        return "not_required"
    if signals.get("synthetic_data"):
        return "skipped_synthetic"
    officers = table_select("officers", filters={"river_id": river["id"]})
    if not officers:
        return "no_officers_mapped"

    recent = table_select("alerts", filters={"river_id": river["id"]}, order_by="detected_at", desc=True, limit=100)
    cutoff = _now() - timedelta(hours=config.NOTIFY_COOLDOWN_HOURS)
    sha = (signals.get("provenance") or {}).get("source_sha256")
    for a in recent:
        if a["id"] == alert["id"]:
            continue
        sent = "sent" in (a.get("whatsapp_status"), a.get("email_status"))
        if not sent:
            continue
        a_sig = a.get("reasoning_signals") or {}
        if isinstance(a_sig, str):
            a_sig = {}
        if sha and (a_sig.get("provenance") or {}).get("source_sha256") == sha:
            return "skipped_duplicate_evidence"
        try:
            when = datetime.fromisoformat(str(a["detected_at"]).replace("Z", "+00:00"))
        except Exception:
            continue
        if (alert["source"] == "satellite" and a.get("source") == "satellite" and when >= cutoff
                and SEVERITY_RANK.get(a["severity"], 0) >= SEVERITY_RANK[alert["severity"]]):
            return "skipped_cooldown"

    for officer in officers:
        statuses = notifier.notify_officer(alert, river["name"], officer)
        if statuses:
            table_update("alerts", alert["id"], statuses)
            alert.update(statuses)
    return "attempted"


def _persist_signals(alert, signals):
    try:
        table_update("alerts", alert["id"], {"reasoning_signals": signals})
    except Exception as e:
        logger.warning(f"Could not persist final signals: {e}")


def _finalize_evidence(alert, river_name, detections, image_paths, extra):
    """PDF + hash + DB update. Failure here must not lose the alert."""
    try:
        pdf_path, sha256 = pdf_generator.generate_evidence_pdf(alert, river_name, detections, image_paths, extra=extra)
        pdf_ref = gcs_storage.upload_pdf(alert["id"], pdf_path) or pdf_path  # keep the local path if GCS is off / failed
        updates = {"evidence_pdf_url": pdf_ref, "evidence_sha256": sha256}
        table_update("alerts", alert["id"], updates)
        alert.update(updates)
    except Exception as e:
        logger.exception("Evidence PDF generation failed")
        alert.setdefault("warnings", []).append(f"Evidence PDF could not be generated: {e}")


# ── image ───────────────────────────────────────────────────────────────
def process_image_upload(image_path, river=None, lat=None, lng=None, lowlight_mode="off", original_name=None):
    """Full pipeline on one uploaded image. Returns the created alert dict (with detections + analysis)."""
    import cv2
    analysis = detector.analyze_image_file(image_path, lowlight_mode=lowlight_mode)
    frame = analysis["frame"]
    dets = analysis["detections"]
    cluster_engine.cluster_pixels(dets, frame_width=frame.shape[1])
    summaries = cluster_engine.summarize_clusters(dets)
    best = _best_cluster(summaries)
    evidence = [d for d in dets if d.get("role") == "evidence"]
    object_counts = dict(Counter(d["class_name"] for d in evidence))

    a_lat, a_lng, loc_src = _locate(river, lat, lng)
    inside, zone_method = _resolve_zone(a_lat, a_lng, river, loc_src)

    alert_id = str(uuid.uuid4())
    model_info = detector.get_model_info()
    src_sha = sha256_file(image_path)
    signals = {
        "inside_buffer": inside, "zone_method": zone_method, "location_source": loc_src,
        "best_cluster": best, "track_behaviors": [], "satellite_change_score": None,
        "input_type": "image", "object_counts": object_counts, "detection_counts": detector.summarize_counts(dets),
        "model_info": model_info,
        "lowlight": analysis["lowlight"],
        "lowlight_comparison": _lowlight_compare(analysis),
        "provenance": {
            "source_name": original_name, "source_sha256": src_sha, "source_bytes": Path(image_path).stat().st_size,
            "image_size": analysis["image_size"], "processed_at": _now().isoformat(),
            "inference_seconds": analysis["inference_seconds"], "pipeline": "yolo+dbscan+rules",
        },
    }
    verdict = reasoning.evaluate_detailed(signals)
    signals["reasoning"] = {"method": verdict["method"], "breakdown": verdict["breakdown"], "caps": verdict["caps"]}

    media = {"original": str(image_path)}
    annotated = detector.draw_detections(frame, dets)
    media["annotated"] = _save_jpg(config.EVIDENCE_DIR / f"annotated_{alert_id}.jpg", annotated)
    pdf_images = [media["annotated"]]
    if analysis["enhanced_frame"] is not None:
        enh_dets = [d for d in dets if d.get("view") in ("enhanced", "both")]
        enh_annot = detector.draw_detections(analysis["enhanced_frame"], enh_dets)
        media["enhanced"] = _save_jpg(config.EVIDENCE_DIR / f"enhanced_{alert_id}.jpg", enh_annot)
        media["comparison"] = _save_jpg(config.EVIDENCE_DIR / f"comparison_{alert_id}.jpg", _comparison_image(annotated, enh_annot))
        pdf_images = [media["comparison"]]
    media = gcs_storage.upload_media(alert_id, media)  # local paths -> gs:// refs when GCS_BUCKET is set (pdf_images stay local)
    signals["media"] = media

    alert = table_insert("alerts", {
        "id": alert_id, "river_id": river["id"] if river else None, "source": "manual_upload",
        "severity": verdict["severity"], "confidence": verdict["score"], "lat": a_lat, "lng": a_lng,
        "inside_buffer": inside, "reasoning_text": verdict["text"], "reasoning_signals": signals,
        "annotated_image_path": media["annotated"], "detected_at": _now().isoformat(),
    })
    _insert_detections(alert["id"], dets)
    _finalize_evidence(alert, river["name"] if river else "Unknown", dets, pdf_images, extra=signals)
    signals["notification"] = _notify_if_needed(alert, river, signals)
    _persist_signals(alert, signals)
    alert["detections"] = dets
    alert["object_counts"] = object_counts
    alert["analysis"] = _public_analysis(signals)
    return alert


def _lowlight_compare(analysis):
    if analysis["enhanced"] is None:
        return None
    o = analysis["original"]
    e = analysis["enhanced"]
    key = lambda L: dict(Counter(d["class_name"] for d in L if d.get("role") == "evidence"))
    return {
        "original_counts": key(o), "enhanced_counts": key(e),
        "merged_counts": key(analysis["detections"]),
        "note": "A change in detection count is NOT evidence of better accuracy: extra detections can be false positives. "
                "Validate on labelled night images before relying on enhancement.",
    }


def _public_analysis(signals):
    return {k: signals.get(k) for k in ("model_info", "lowlight", "lowlight_comparison", "provenance", "reasoning",
                                         "zone_method", "location_source", "detection_counts", "object_counts",
                                         "video", "notification") if signals.get(k) is not None}


# ── video ───────────────────────────────────────────────────────────────
def process_video_upload(video_path, river=None, lat=None, lng=None, lowlight_mode="off", original_name=None):
    """Detection + tracking across sampled frames, per-frame clustering, aggregate reasoning."""
    analysis = detector.analyze_video_file(video_path, lowlight_mode=lowlight_mode)
    dets = analysis["detections"]
    bf = analysis["best_frame"]
    frame_w = bf["frame"].shape[1] if bf else None
    cluster_engine.cluster_per_frame(dets, frame_width=frame_w)
    summaries = cluster_engine.summarize_clusters(dets)
    best = _best_cluster(summaries)
    tracks = analysis["track_summaries"]
    evidence_tracks = [t for t in tracks if t["class_name"] in detector.EVIDENCE_CLASSES]
    object_counts = dict(Counter(t["class_name"] for t in evidence_tracks))  # UNIQUE tracked objects

    a_lat, a_lng, loc_src = _locate(river, lat, lng)
    inside, zone_method = _resolve_zone(a_lat, a_lng, river, loc_src)

    alert_id = str(uuid.uuid4())
    src_sha = sha256_file(video_path)
    signals = {
        "inside_buffer": inside, "zone_method": zone_method, "location_source": loc_src,
        "best_cluster": best, "track_behaviors": tracks, "satellite_change_score": None,
        "input_type": "video", "num_tracked_objects": len(tracks),
        "object_counts": object_counts, "detection_counts": detector.summarize_counts(dets),
        "model_info": detector.get_model_info(),
        "lowlight": analysis["lowlight"],
        "video": {k: analysis[k] for k in ("video_fps", "duration_s", "frames_analyzed", "sample_fps", "truncated", "max_seconds")},
        "provenance": {"source_name": original_name, "source_sha256": src_sha, "source_bytes": Path(video_path).stat().st_size,
                       "processed_at": _now().isoformat(), "pipeline": "yolo+iou-tracker+dbscan(per-frame)+rules"},
    }
    if bf:
        signals["video"]["best_frame"] = {"frame_number": bf["frame_number"], "timestamp_s": bf["timestamp_s"]}
    verdict = reasoning.evaluate_detailed(signals)
    signals["reasoning"] = {"method": verdict["method"], "breakdown": verdict["breakdown"], "caps": verdict["caps"]}

    media, pdf_images = {"original": str(video_path)}, []
    if bf:
        frame_dets = bf["detections"]  # ONLY detections that belong to this frame
        annotated = detector.draw_detections(bf["frame"], frame_dets, title=f"t={bf['timestamp_s']}s (frame {bf['frame_number']})")
        media["annotated"] = _save_jpg(config.EVIDENCE_DIR / f"annotated_{alert_id}.jpg", annotated)
        pdf_images = [media["annotated"]]
        if bf["enhanced_frame"] is not None:
            enh_annot = detector.draw_detections(bf["enhanced_frame"], [d for d in frame_dets if d.get("view") in ("enhanced", "both")])
            media["enhanced"] = _save_jpg(config.EVIDENCE_DIR / f"enhanced_{alert_id}.jpg", enh_annot)
            media["comparison"] = _save_jpg(config.EVIDENCE_DIR / f"comparison_{alert_id}.jpg", _comparison_image(annotated, enh_annot))
            pdf_images = [media["comparison"]]
    media = gcs_storage.upload_media(alert_id, media)  # video originals are never uploaded (only image files are)
    signals["media"] = media

    alert = table_insert("alerts", {
        "id": alert_id, "river_id": river["id"] if river else None, "source": "manual_upload",
        "severity": verdict["severity"], "confidence": verdict["score"], "lat": a_lat, "lng": a_lng,
        "inside_buffer": inside, "reasoning_text": verdict["text"], "reasoning_signals": signals,
        "annotated_image_path": media.get("annotated"), "detected_at": _now().isoformat(),
    })
    _insert_detections(alert["id"], dets)
    _finalize_evidence(alert, river["name"] if river else "Unknown", dets, pdf_images or None, extra=signals)
    signals["notification"] = _notify_if_needed(alert, river, signals)
    _persist_signals(alert, signals)
    alert["detections"] = dets[:500]
    alert["detections_total"] = len(dets)
    alert["object_counts"] = object_counts
    alert["track_behaviors"] = tracks
    alert["analysis"] = _public_analysis(signals)
    return alert


# ── satellite ───────────────────────────────────────────────────────────
def _monitor_points_for_river(river):
    stored = river.get("monitor_points")
    if stored and isinstance(stored, list) and len(stored) > 0:
        return stored
    from satellite.river_corridor import fetch_monitor_points
    return fetch_monitor_points(river["center_lat"], river["center_lng"], river_name=river.get("name"))


def process_satellite_scan(river, before_date=None, after_date=None):
    """
    Satellite tier for one river. Scans up to 10 on-river points IN PARALLEL, scores each against the
    river-wide background, and raises ONE alert at the hotspot. If no point yields a valid, distinct
    before/after pair, raises NoSatelliteData (no alert, no invented numbers).
    """
    from satellite import gee_client, scoring

    after_date = after_date or _now().strftime("%Y-%m-%d")
    points = [p for p in _monitor_points_for_river(river) if p.get("on_river", True)][:10]
    if not points:
        raise NoSatelliteData("This river has no corridor monitor points. Use 'refresh corridor' or re-add the river.")

    def one(pt):
        r = gee_client.get_before_after_pair(pt["lat"], pt["lng"], before_date, after_date)
        return {"lat": pt["lat"], "lng": pt["lng"], "label": pt.get("label", "point"), "on_river": True, **r}

    with ThreadPoolExecutor(max_workers=4) as ex:
        scan_results = list(ex.map(one, points))

    synthetic = any(r.get("synthetic") for r in scan_results)
    if synthetic:  # GEE credentials absent: synthetic demo values already carry change_score
        for r in scan_results:
            r.setdefault("change_score", r.get("change_score", 0.0))
        hotspot = max(scan_results, key=lambda r: r.get("change_score") or 0)
    else:
        scoring.corridor_scores(scan_results)
        hotspot = scoring.pick_hotspot(scan_results)
        if hotspot is None:
            reasons = Counter(r.get("status", "error") for r in scan_results)
            sample = next((r.get("message") for r in scan_results if r.get("message")), "no details")
            raise NoSatelliteData(
                "No valid Sentinel-2 before/after comparison could be made for any corridor point "
                f"({dict(reasons)}). Last detail: {sample}",
                details=[{"label": r["label"], "status": r.get("status"), "message": r.get("message")} for r in scan_results])

    bs, as_ = hotspot.get("before_scene") or {}, hotspot.get("after_scene") or {}
    usable = [r for r in scan_results if r.get("change_score") is not None]
    rain_mm = None
    if not synthetic and bs and as_:
        rain_mm = gee_client.rainfall_between(hotspot["lat"], hotspot["lng"], bs["date"], as_["date"])
    rain_confounder = bool(rain_mm is not None and rain_mm >= config.SAT_RAIN_CONFOUNDER_MM)

    signals = {
        "inside_buffer": None, "best_cluster": None, "track_behaviors": [],
        "satellite_change_score": hotspot.get("change_score"),
        "raw_ndwi_diff": hotspot.get("mean_ndwi_diff"),
        "input_type": "satellite", "synthetic_data": synthetic,
        "before_date": bs.get("date"), "after_date": as_.get("date"),
        "before_scene": bs or None, "after_scene": as_ or None,
        "corridor_points_scanned": len(scan_results), "corridor_points_usable": len(usable),
        "hotspot_label": hotspot.get("label"), "method": hotspot.get("method"), "mask_source": hotspot.get("mask_source"),
        "changed_fraction": hotspot.get("changed_fraction"), "excess_change": hotspot.get("excess_change"),
        "background_changed_fraction": hotspot.get("background_changed_fraction"),
        "background_available": hotspot.get("background_available"),
        "metrics": {k: hotspot.get(k) for k in ("analysed_area_m2", "water_before_m2", "water_after_m2", "bare_before_m2",
                                                "bare_after_m2", "bare_gain_m2", "bare_loss_m2")},
        "rain_mm_between_scenes": rain_mm, "rain_confounder": rain_confounder,
        "scan_results": [{
            "lat": r["lat"], "lng": r["lng"], "label": r.get("label"), "status": r.get("status"),
            "change_score": r.get("change_score"), "raw_ndwi_diff": r.get("mean_ndwi_diff"),
            "changed_fraction": r.get("changed_fraction"), "excess_change": r.get("excess_change"),
            "synthetic": bool(r.get("synthetic")), "on_river": True,
            "before_date": (r.get("before_scene") or {}).get("date"), "after_date": (r.get("after_scene") or {}).get("date"),
            "message": r.get("message") if r.get("status") != "ok" else None,
        } for r in scan_results],
        "provenance": {"processed_at": _now().isoformat(), "source": "Sentinel-2 SR Harmonized via Google Earth Engine" if not synthetic else "SYNTHETIC",
                       "pipeline": "ndwi-river-bed-mask+corridor-background+rules"},
    }
    verdict = reasoning.evaluate_detailed(signals)
    signals["reasoning"] = {"method": verdict["method"], "breakdown": verdict["breakdown"], "caps": verdict["caps"]}
    text = verdict["text"]
    if synthetic:
        text += " SYNTHETIC DEMO DATA: Google Earth Engine is not configured, so this is not a real observation."
    else:
        text += (f" Real Sentinel-2 comparison: before {bs.get('date')} vs after {as_.get('date')} "
                 f"({len(usable)}/{len(scan_results)} corridor points usable; hotspot: {hotspot.get('label')}).")
        if not hotspot.get("background_available"):
            text += " Fewer than 3 usable points: river-wide background could not be removed, so natural change is not controlled for."

    alert_id = str(uuid.uuid4())
    before_local = after_local = None
    if not synthetic and bs and as_:
        try:
            b_url, a_url = gee_client.thumbnails(hotspot["lat"], hotspot["lng"], bs["id"], as_["id"])
            before_local, after_local = config.EVIDENCE_DIR / f"before_{alert_id}.jpg", config.EVIDENCE_DIR / f"after_{alert_id}.jpg"
            if not (gee_client.download_thumbnail(b_url, before_local) and gee_client.download_thumbnail(a_url, after_local)):
                before_local = after_local = None
        except Exception as e:
            logger.warning(f"Thumbnail generation failed: {e}")
            before_local = after_local = None

    before_ref, after_ref = (str(before_local) if before_local else None), (str(after_local) if after_local else None)
    if before_local and after_local:
        refs = gcs_storage.upload_media(alert_id, {"before": str(before_local), "after": str(after_local)})
        before_ref, after_ref = refs["before"], refs["after"]

    alert = table_insert("alerts", {
        "id": alert_id, "river_id": river["id"], "source": "satellite", "severity": verdict["severity"],
        "confidence": verdict["score"], "lat": hotspot["lat"], "lng": hotspot["lng"], "inside_buffer": True,
        "reasoning_text": text, "reasoning_signals": signals,
        "before_image_url": before_ref, "after_image_url": after_ref, "detected_at": _now().isoformat(),
    })
    _finalize_evidence(alert, river["name"], [], [p for p in (before_local, after_local) if p] or None, extra=signals)
    signals["notification"] = _notify_if_needed(alert, river, signals)
    _persist_signals(alert, signals)
    alert["analysis"] = _public_analysis(signals)
    return alert
