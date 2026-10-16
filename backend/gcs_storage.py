"""
Google Cloud Storage for evidence media (annotated / enhanced / comparison / before / after images,
the original uploaded image, and the evidence PDF).

Why: Railway's container disk is wiped on every deploy, so files written to EVIDENCE_DIR vanish and the
website shows broken images. With GCS_BUCKET set, every file is also uploaded to the bucket, the database
stores a stable reference ("gs://<bucket>/alerts/<alert_id>/<name>") and the Flask API streams the bytes
back from the bucket - so the front end keeps using the same /api/alerts/<id>/before.jpg style URLs.

Nothing here is required: when GCS_BUCKET is empty (or an upload fails) the app behaves exactly as before
and keeps using local files.

Auth: the same service-account JSON you already use for Earth Engine (GEE_PRIVATE_KEY_JSON), or a dedicated
GCS_PRIVATE_KEY_JSON. That service account needs the "Storage Object Admin" role on the bucket.
"""
import logging
import threading
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_client = None
_bucket = None
_lock = threading.Lock()

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp"}
_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".bmp": "image/bmp",
                  ".pdf": "application/pdf"}

# Kinds that are always JPEG and have a deterministic object name (so the API can fetch them without a DB lookup).
FIXED_KINDS = {
    "annotated": "annotated.jpg",
    "enhanced": "enhanced.jpg",
    "comparison": "comparison.jpg",
    "before": "before.jpg",
    "after": "after.jpg",
}


def enabled():
    return bool(config.GCS_BUCKET)


def _get_bucket():
    """Lazily creates one shared client/bucket handle (google-cloud-storage clients are thread-safe)."""
    global _client, _bucket
    if _bucket is not None:
        return _bucket
    with _lock:
        if _bucket is not None:
            return _bucket
        from google.cloud import storage
        key_path = config.GCS_KEY_PATH
        if key_path and Path(key_path).exists():
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(key_path)
            _client = storage.Client(project=config.GCS_PROJECT_ID or creds.project_id, credentials=creds)
        else:
            _client = storage.Client(project=config.GCS_PROJECT_ID or None)  # Application Default Credentials
        _bucket = _client.bucket(config.GCS_BUCKET)
        return _bucket


def blob_name(alert_id, kind, ext=""):
    """alerts/<alert_id>/<file>. kind in FIXED_KINDS, 'original' (needs ext) or 'pdf'."""
    if kind in FIXED_KINDS:
        return f"alerts/{alert_id}/{FIXED_KINDS[kind]}"
    if kind == "pdf":
        return f"alerts/{alert_id}/evidence.pdf"
    if kind == "original":
        return f"alerts/{alert_id}/original{ext or '.jpg'}"
    raise ValueError(f"unknown media kind: {kind}")


def make_ref(name):
    return f"gs://{config.GCS_BUCKET}/{name}"


def is_ref(value):
    return isinstance(value, str) and value.startswith("gs://")


def _ref_to_name(ref):
    """Only accepts references to OUR bucket (a DB value can never make the API read another bucket)."""
    prefix = f"gs://{config.GCS_BUCKET}/"
    if config.GCS_BUCKET and isinstance(ref, str) and ref.startswith(prefix):
        return ref[len(prefix):]
    return None


def upload_file(local_path, name, content_type=None):
    """Uploads one local file. Returns the gs:// reference, or None on any failure (caller keeps the local path)."""
    if not enabled():
        return None
    try:
        p = Path(local_path)
        if not p.exists():
            return None
        ctype = content_type or _CONTENT_TYPES.get(p.suffix.lower(), "application/octet-stream")
        blob = _get_bucket().blob(name)
        blob.upload_from_filename(str(p), content_type=ctype, timeout=120)
        return make_ref(name)
    except Exception as e:  # noqa: BLE001 - storage problems must never lose an alert
        logger.warning(f"GCS upload failed for {name}: {e}")
        return None


def upload_media(alert_id, media):
    """
    media: {kind: local_path}. Returns the same dict where every successfully uploaded file is replaced by its
    gs:// reference (failures keep the local path, so behaviour degrades to the old local-disk mode).
    Videos are never uploaded (they can be hundreds of MB); only image originals are.
    """
    out = dict(media)
    if not enabled():
        return out
    for kind, path in media.items():
        if not path:
            continue
        ext = Path(str(path)).suffix.lower()
        if kind == "original":
            if ext not in IMAGE_EXT:
                continue
            name = blob_name(alert_id, "original", ext)
        elif kind in FIXED_KINDS:
            name = blob_name(alert_id, kind)
        else:
            continue
        ref = upload_file(path, name)
        if ref:
            out[kind] = ref
    return out


def upload_pdf(alert_id, local_path):
    return upload_file(local_path, blob_name(alert_id, "pdf"), "application/pdf")


def download_name(name):
    """Returns (bytes, content_type) or None when missing/unavailable."""
    if not enabled():
        return None
    try:
        blob = _get_bucket().blob(name)
        data = blob.download_as_bytes(timeout=30)
        ctype = blob.content_type or _CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")
        return data, ctype
    except Exception as e:  # noqa: BLE001
        # google.api_core.exceptions.NotFound is the normal "not there" case - keep it quiet.
        if e.__class__.__name__ == "NotFound":
            return None
        logger.warning(f"GCS download failed for {name}: {e}")
        return None


def download_ref(ref):
    name = _ref_to_name(ref)
    return download_name(name) if name else None


def probe():
    """Writes, reads and deletes a tiny object. Used by /api/system-status?probe=1 to diagnose permissions."""
    if not enabled():
        return {"ok": False, "error": "GCS_BUCKET is not set"}
    try:
        blob = _get_bucket().blob("_riverguard_probe/ping.txt")
        blob.upload_from_string(b"ok", content_type="text/plain", timeout=30)
        data = blob.download_as_bytes(timeout=30)
        blob.delete()
        return {"ok": data == b"ok", "bucket": config.GCS_BUCKET}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "bucket": config.GCS_BUCKET, "error": f"{e.__class__.__name__}: {e}"[:400]}
