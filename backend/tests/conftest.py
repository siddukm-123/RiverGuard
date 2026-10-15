"""Shared fixtures. Tests run with in-memory storage, no scheduler and notification dry-run."""
import os
import sys
from pathlib import Path

os.environ.setdefault("ENABLE_SCHEDULER", "false")
os.environ.setdefault("RG_NOTIFY_DRY_RUN", "true")
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["GEE_SERVICE_ACCOUNT"] = ""
os.environ["GEE_PRIVATE_KEY_JSON"] = ""
os.environ["GEE_PRIVATE_KEY_PATH"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["RIVERGUARD_API_KEY"] = ""
import tempfile
_tmp = tempfile.mkdtemp(prefix="rg-test-")
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.environ["EVIDENCE_DIR"] = os.path.join(_tmp, "evidence")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


def yolo_weights():
    """Path to real yolov8n weights if present (env YOLO_MODEL_PATH or ./yolov8n.pt); else None."""
    for c in (os.getenv("YOLO_MODEL_PATH"), "yolov8n.pt", str(Path(__file__).resolve().parents[2] / "models" / "yolov8n.pt")):
        if c and Path(c).exists():
            return c
    return None


needs_weights = pytest.mark.skipif(yolo_weights() is None, reason="yolov8n.pt weights not available (SKIPPED, not passed)")


@pytest.fixture(scope="session")
def sample_image(tmp_path_factory):
    """ultralytics ships bus.jpg (3 persons + 1 bus). Skips if the package or file is absent."""
    try:
        import ultralytics
    except Exception:
        pytest.skip("ultralytics not installed")
    p = Path(ultralytics.__file__).parent / "assets" / "bus.jpg"
    if not p.exists():
        pytest.skip("bundled sample image not found")
    return p


@pytest.fixture()
def client():
    import app as appmod
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()
