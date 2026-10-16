"""
Central configuration. Every secret is read from environment variables so
nothing sensitive ever gets committed. Copy .env.example to .env and fill
in real values, or export these in your shell / hosting provider.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; env vars can also be set directly

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Supabase ────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")  # use the service_role key on the backend, never expose it to the browser

# ── Detection ───────────────────────────────────────────────────────────
YOLO_MODEL_PATH = os.getenv("YOLO_MODEL_PATH", "yolov8n.pt")  # pretrained COCO model: detects person + truck out of the box
CUSTOM_JCB_MODEL_PATH = os.getenv("CUSTOM_JCB_MODEL_PATH", "")  # optional: path to a model you fine-tuned on JCB/excavator images (see README)
DETECTION_CONF_THRESHOLD = float(os.getenv("DETECTION_CONF_THRESHOLD", "0.35"))
VIDEO_SAMPLE_FPS = float(os.getenv("VIDEO_SAMPLE_FPS", "2"))  # how many frames/sec to actually run detection on for uploaded video

VIDEO_MAX_SECONDS = float(os.getenv("VIDEO_MAX_SECONDS", "600"))  # hard cap on analysed video duration (protects CPU/RAM on small hosts)
# COCO 'car'/'bus' are NOT trucks. Folding them into 'truck' (old behaviour) inflates the truck signal.
# Set FOLD_VEHICLES_AS_TRUCK=true only if you deliberately want the legacy behaviour.
FOLD_VEHICLES_AS_TRUCK = os.getenv("FOLD_VEHICLES_AS_TRUCK", "false").lower() == "true"
# Class-name fragments (lower-case) of a custom model that count as excavation equipment ('jcb' bucket).
EXCAVATOR_CLASS_KEYWORDS = [k.strip() for k in os.getenv(
    "EXCAVATOR_CLASS_KEYWORDS", "excavator,jcb,backhoe,digger,dredger,bulldozer,loader").split(",") if k.strip()]

# ── Low-light (software enhancement only — NOT thermal / infrared night vision) ──
LOWLIGHT_DARK_LUMA_THRESHOLD = float(os.getenv("LOWLIGHT_DARK_LUMA_THRESHOLD", "70"))  # mean luma (0-255) below this = "dark"

# ── Reasoning engine ────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # optional — if unset, falls back to rule-based reasoning
REASONING_MODEL = os.getenv("REASONING_MODEL", "claude-sonnet-4-6")

# ── Alerts ──────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")  # e.g. 'whatsapp:+14155238886' (Twilio sandbox number)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# ── Satellite (Google Earth Engine) ─────────────────────────────────────
GEE_SERVICE_ACCOUNT = os.getenv("GEE_SERVICE_ACCOUNT", "")
GEE_PROJECT_ID = os.getenv("GEE_PROJECT_ID", "")


def _materialize_gee_key():
    """
    Railway/cloud: paste the full service-account JSON into GEE_PRIVATE_KEY_JSON.
    Local dev: point GEE_PRIVATE_KEY_PATH at secrets/gee-key.json instead.
    """
    json_inline = os.getenv("GEE_PRIVATE_KEY_JSON", "").strip()
    if json_inline:
        path = Path(os.getenv("GEE_PRIVATE_KEY_PATH", "/tmp/gee-key.json"))
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json_inline, encoding="utf-8")
        return str(path)
    return os.getenv("GEE_PRIVATE_KEY_PATH", "")


GEE_PRIVATE_KEY_PATH = _materialize_gee_key()

# ── Google Cloud Storage (evidence images / PDFs) ───────────────────────
# Railway's disk is wiped on every deploy. Set GCS_BUCKET to keep images + PDFs in a bucket instead.
# Credentials: the same service-account JSON as Earth Engine (GEE_PRIVATE_KEY_JSON) unless you paste a
# dedicated one into GCS_PRIVATE_KEY_JSON. The account needs "Storage Object Admin" on the bucket.
GCS_BUCKET = os.getenv("GCS_BUCKET", "").strip()
GCS_PROJECT_ID = os.getenv("GCS_PROJECT_ID", "").strip() or GEE_PROJECT_ID


def _materialize_gcs_key():
    json_inline = os.getenv("GCS_PRIVATE_KEY_JSON", "").strip()
    if json_inline:
        path = Path("/tmp/gcs-key.json")
        if not path.exists():
            path.write_text(json_inline, encoding="utf-8")
        return str(path)
    return GEE_PRIVATE_KEY_PATH  # reuse the Earth Engine service account


GCS_KEY_PATH = _materialize_gcs_key()

# Satellite pairing rules (see satellite/gee_client.py). All optional — defaults are scientifically safer than the old +-30 day window.
SAT_BASELINE_DAYS = int(os.getenv("SAT_BASELINE_DAYS", "30"))             # 'before' target = 'after' scene date - this many days
SAT_MIN_SEPARATION_DAYS = int(os.getenv("SAT_MIN_SEPARATION_DAYS", "10"))  # two scenes closer than this are rejected as a pair
SAT_SEARCH_HALF_WINDOW_DAYS = int(os.getenv("SAT_SEARCH_HALF_WINDOW_DAYS", "8"))
SAT_MIN_VALID_FRACTION = float(os.getenv("SAT_MIN_VALID_FRACTION", "0.6"))  # min cloud-free pixel fraction inside the AOI
SAT_RAIN_CONFOUNDER_MM = float(os.getenv("SAT_RAIN_CONFOUNDER_MM", "50"))  # rainfall between scenes above this flags a natural-change confounder

# ── Alerts hygiene ───────────────────────────────────────────────────────
NOTIFY_COOLDOWN_HOURS = float(os.getenv("NOTIFY_COOLDOWN_HOURS", "24"))  # don't re-notify the same river/source/severity inside this window
# HIGH needs corroboration (excavator in frame, or satellite + ground evidence). Trucks/people alone cap at WATCH.
HIGH_REQUIRES_CORROBORATION = os.getenv("HIGH_REQUIRES_CORROBORATION", "true").lower() == "true"

# ── Access control (optional) ────────────────────────────────────────────
# If set, every non-GET /api request (upload, scan, add/delete river...) must carry header X-API-Key: <value>.
RIVERGUARD_API_KEY = os.getenv("RIVERGUARD_API_KEY", "")

# ── Zone / buffer ────────────────────────────────────────────────────────
DEFAULT_BUFFER_RADIUS_M = float(os.getenv("DEFAULT_BUFFER_RADIUS_M", "1000"))

# ── Misc ─────────────────────────────────────────────────────────────────
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(PROJECT_ROOT / "data" / "uploads")))
EVIDENCE_DIR = Path(os.getenv("EVIDENCE_DIR", str(PROJECT_ROOT / "data" / "evidence")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
FLASK_PORT = int(os.getenv("PORT", os.getenv("FLASK_PORT", "8000")))
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
CORS_ORIGIN = os.getenv("CORS_ORIGIN", "*")  # set to your Vercel URL in production
