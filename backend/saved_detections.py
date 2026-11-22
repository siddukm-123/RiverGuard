"""
Saved manual detections.

Every file uploaded on the Manual Detection page is kept and shown in Details -> "Manual detections", under a name the user
chooses (or the file name). For a video we keep:
  * the video itself        -> Google Cloud Storage (alerts/<id>/video.mp4), or the server disk if no bucket is configured,
  * the detection boxes     -> a small JSON "overlay" (alerts/<id>/overlay.json), so the page can replay the video with the
                               boxes drawn on it,
  * the name + the result   -> inside the alert's reasoning_signals (no database migration needed).
Images already keep their annotated picture and evidence PDF through the normal pipeline.

Nothing here can fail an upload: problems become warnings.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import config
import gcs_storage
from api.supabase_client import table_update

logger = logging.getLogger(__name__)

VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska"}
MAX_NAME_LEN = 120


def clean_name(name, original_name=None):
    """The user's name (whitespace tidied), else the file name without its extension, else ''."""
    n = " ".join(str(name or "").split())[:MAX_NAME_LEN]
    if n:
        return n
    return " ".join(Path(str(original_name or "")).stem.split())[:MAX_NAME_LEN]


def persist(alert, save_path, is_image, name, original_name):
    """Keeps the uploaded media, stores the overlay and records the name on the alert. Returns {"name", "warnings", "video_stored"}."""
    warnings = []
    aid = alert["id"]
    signals = dict(alert.get("reasoning_signals") or {})
    media = dict(signals.get("media") or {})
    video_stored = False

    if not is_image:
        ext = Path(str(save_path)).suffix.lower()
        ref = gcs_storage.upload_file(save_path, gcs_storage.blob_name(aid, "video", ext), VIDEO_TYPES.get(ext))
        if ref:
            media["video"], video_stored = ref, True
        elif Path(str(save_path)).exists():
            media["video"], video_stored = str(save_path), True            # kept on the server disk
            warnings.append("The video could not be copied to cloud storage" if gcs_storage.enabled()
                            else "No storage bucket is configured, so the video is kept on the server disk and will be lost at the next redeploy")
        else:
            warnings.append("The uploaded video was no longer available to save.")

        overlay = alert.get("video_overlay")
        if overlay:
            try:
                data = json.dumps(overlay, separators=(",", ":")).encode()
                oref = gcs_storage.upload_bytes(data, gcs_storage.blob_name(aid, "overlay"), "application/json")
                if not oref:
                    local = Path(config.EVIDENCE_DIR) / f"{aid}_overlay.json"
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_bytes(data)
                    oref = str(local)
                media["overlay"] = oref
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Could not store the box overlay for {aid}: {e}")
                warnings.append("The detection boxes could not be saved, so the video will play without boxes.")

    final_name = clean_name(name, original_name)
    signals.update({"detection_name": final_name, "original_filename": str(original_name or "")[:255],
                    "file_kind": "image" if is_image else "video", "saved_at": datetime.now(timezone.utc).isoformat(), "media": media})
    try:
        table_update("alerts", aid, {"reasoning_signals": signals})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not record the name for {aid}: {e}")
        warnings.append("The name could not be saved in the database.")
    alert["reasoning_signals"] = signals
    return {"name": final_name, "warnings": warnings, "video_stored": video_stored}
