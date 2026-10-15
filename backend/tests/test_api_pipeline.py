"""API + pipeline integration tests (Flask test client, in-memory DB, dry-run notifications).
Tests needing real YOLO weights are SKIPPED (never passed) when the weights are absent."""
import io
import os
import time

import cv2
import numpy as np
import pytest

import config
from conftest import needs_weights, yolo_weights

if yolo_weights():
    config.YOLO_MODEL_PATH = yolo_weights()


def _river(client):
    """Seed river with offline corridor points (Overpass is not reachable in CI/sandbox)."""
    from api.supabase_client import table_update
    r = client.get("/api/rivers").get_json()[0]
    if not r.get("monitor_points"):
        pts = [{"lat": r["center_lat"] + i * 0.01, "lng": r["center_lng"], "label": f"p{i}", "on_river": True} for i in range(5)]
        r = table_update("rivers", r["id"], {"monitor_points": pts})
    return r


def test_health_and_system_status(client):
    assert client.get("/api/health").get_json() == {"status": "ok"}
    st = client.get("/api/system-status").get_json()
    assert st["storage"] == "memory" and st["storage_persistent"] is False
    assert st["satellite"] == "synthetic_demo_only"
    assert st["model"]["excavator_detection_available"] is False


def test_json_errors_are_consistent(client):
    r = client.post("/api/upload", data={}, content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["code"] == "no_file"
    r = client.post("/api/upload", data={"file": (io.BytesIO(b"x"), "a.exe")}, content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["code"] == "unsupported_type"
    r = client.post("/api/upload", data={"file": (io.BytesIO(b"<html>"), "a.jpg")}, content_type="multipart/form-data")
    assert r.status_code == 422 and r.get_json()["code"] == "invalid_media"
    assert client.get("/api/nope").status_code == 404 and client.get("/api/nope").get_json()["code"] == "not_found"
    assert client.post("/api/satellite-scan", data="notjson", content_type="text/plain").status_code == 400
    assert client.post("/api/satellite-scan", json={"river_id": "x"}).status_code == 404
    assert client.get("/api/alerts?severity=BAD").status_code == 400


def test_corrupt_image_with_valid_header_is_422(client):
    r = client.post("/api/upload", data={"file": (io.BytesIO(b"\xff\xd8\xff" + b"garbage" * 50), "a.jpg")}, content_type="multipart/form-data")
    assert r.status_code == 422


def test_oversize_upload_json(client, monkeypatch):
    import app as appmod
    monkeypatch.setitem(appmod.app.config, "MAX_CONTENT_LENGTH", 100)
    r = client.post("/api/upload", data={"file": (io.BytesIO(b"\xff\xd8\xff" + b"0" * 5000), "a.jpg")}, content_type="multipart/form-data")
    assert r.status_code == 413 and r.get_json()["code"] == "payload_too_large"


def test_write_protection(client, monkeypatch):
    monkeypatch.setattr(config, "RIVERGUARD_API_KEY", "secret")
    assert client.post("/api/satellite-scan", json={}).status_code == 401
    assert client.get("/api/rivers").status_code == 200
    assert client.post("/api/satellite-scan", json={}, headers={"X-API-Key": "secret"}).status_code == 400


def test_satellite_scan_synthetic_is_flagged_and_never_notified(client):
    r = client.post("/api/satellite-scan", json={"river_id": _river(client)["id"]})
    assert r.status_code == 201
    a = r.get_json()
    sig = a["reasoning_signals"]
    assert sig["synthetic_data"] is True and "SYNTHETIC" in a["reasoning_text"]
    assert sig["notification"] in ("skipped_synthetic", "no_officers_mapped", "not_required")
    assert a["whatsapp_status"] if "whatsapp_status" in a else True
    assert client.get(f"/api/alerts/{a['id']}/evidence.pdf").status_code == 200


def test_satellite_no_data_creates_no_alert(client, monkeypatch):
    from satellite import gee_client
    monkeypatch.setattr(gee_client, "get_before_after_pair", lambda *a, **k: {"status": "no_scene", "synthetic": False, "message": "cloudy"})
    n = len(client.get("/api/alerts").get_json())
    r = client.post("/api/satellite-scan", json={"river_id": _river(client)["id"]})
    j = r.get_json()
    assert r.status_code == 200 and j["status"] == "no_data" and "cloudy" in j["message"]
    assert len(client.get("/api/alerts").get_json()) == n


def test_satellite_real_path_with_mocked_gee(client, monkeypatch):
    """MOCKED Earth Engine: verifies pipeline wiring, scoring and PDF - not GEE itself."""
    from satellite import gee_client
    def fake(lat, lng, before, after):
        hot = abs(lat - _river(client)["center_lat"]) < 1e-9
        return {"status": "ok", "synthetic": False, "method": "ndwi_river_bed_mask_v2", "mask_source": "mock",
                "changed_fraction": 0.5 if hot else 0.05, "mean_ndwi_diff": -0.2,
                "before_scene": {"id": "B", "date": "2026-07-01", "valid": 0.9}, "after_scene": {"id": "A", "date": "2026-08-01", "valid": 0.95}}
    monkeypatch.setattr(gee_client, "get_before_after_pair", fake)
    monkeypatch.setattr(gee_client, "rainfall_between", lambda *a: 5.0)
    monkeypatch.setattr(gee_client, "thumbnails", lambda *a: (_ for _ in ()).throw(RuntimeError("no net")))
    river = _river(client)
    r = client.post("/api/satellite-scan", json={"river_id": river["id"]})
    a = r.get_json()
    assert r.status_code == 201 and a["reasoning_signals"]["synthetic_data"] is False
    assert a["reasoning_signals"]["before_date"] == "2026-07-01" and a["severity"] in ("WATCH", "SAFE")
    assert a["severity"] != "HIGH"
    assert client.get(f"/api/alerts/{a['id']}/evidence/verify").get_json()["match"] is True


def test_async_job_flow(client, monkeypatch):
    r = client.post("/api/satellite-scan", json={"river_id": _river(client)["id"], "async": True})
    assert r.status_code == 202
    jid = r.get_json()["job_id"]
    for _ in range(50):
        j = client.get(f"/api/jobs/{jid}").get_json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert j["status"] == "done" and j["result"]["status"] == "ok"
    assert client.get("/api/jobs/doesnotexist").status_code == 404


def test_river_create_validation(client):
    assert client.post("/api/rivers", json={"name": "x", "center_lat": 999, "center_lng": 1}).status_code == 400
    assert client.post("/api/rivers", json={"center_lat": 1, "center_lng": 1}).status_code == 400


def test_notification_cooldown_and_synthetic_rules(monkeypatch):
    import pipeline
    from alerts import notifier
    from api.supabase_client import table_insert
    river = table_insert("rivers", {"name": "T", "center_lat": 1, "center_lng": 1})
    table_insert("officers", {"river_id": river["id"], "name": "O", "email": "o@x.in", "whatsapp_number": "+911"})
    monkeypatch.setattr(config, "SMTP_HOST", "h"); monkeypatch.setattr(config, "SMTP_USER", "u"); monkeypatch.setattr(config, "SMTP_PASSWORD", "p")
    prev = table_insert("alerts", {"river_id": river["id"], "source": "satellite", "severity": "WATCH", "email_status": "sent",
                                   "detected_at": pipeline._now().isoformat(), "reasoning_signals": {}})
    new = table_insert("alerts", {"river_id": river["id"], "source": "satellite", "severity": "WATCH", "detected_at": pipeline._now().isoformat()})
    assert pipeline._notify_if_needed(new, river, {}) == "skipped_cooldown"
    assert pipeline._notify_if_needed(new, river, {"synthetic_data": True}) == "skipped_synthetic"
    assert pipeline._notify_if_needed({**new, "severity": "SAFE"}, river, {}) == "not_required"


def test_notifier_unconfigured_does_not_report_failure():
    from alerts import notifier
    st = notifier.notify_officer({"severity": "HIGH", "id": "1"}, "R", {"email": "a@b.c", "whatsapp_number": "+91"})
    assert st == {}  # nothing attempted -> status stays 'not_sent'
    assert notifier._e164("whatsapp:+9199") == "+9199"


@needs_weights
def test_real_yolo_image_upload_end_to_end(client, sample_image):
    river = _river(client)
    with open(sample_image, "rb") as f:
        r = client.post("/api/upload", data={"file": (f, "bus.jpg"), "river_id": river["id"], "lowlight": "auto"}, content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    a = r.get_json()
    assert a["object_counts"].get("person", 0) >= 2            # real detections from real weights
    assert not any(d["class_name"] == "truck" for d in a["detections"])  # bus must NOT be folded into truck
    assert any(d["class_name"] == "bus" and d["role"] == "context" for d in a["detections"])
    assert a["severity"] == "SAFE" and a["inside_buffer"] is None       # river-centre default is not a measured location
    assert a["analysis"]["lowlight"]["applied"] is False                # daylight untouched in auto mode
    assert client.get(f"/api/alerts/{a['id']}/image/annotated").status_code == 200
    assert client.get(f"/api/alerts/{a['id']}/evidence/verify").get_json()["match"] is True
    pdf = client.get(f"/api/alerts/{a['id']}/evidence.pdf").data
    assert pdf[:5] == b"%PDF-"


@needs_weights
def test_real_yolo_lowlight_produces_comparison(client, sample_image):
    img = cv2.imread(str(sample_image))
    dark = np.clip(img.astype(np.float32) * 0.12, 0, 255).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", dark)
    r = client.post("/api/upload", data={"file": (io.BytesIO(buf.tobytes()), "dark.jpg"), "lowlight": "auto"}, content_type="multipart/form-data")
    a = r.get_json()
    assert r.status_code == 201 and a["analysis"]["lowlight"]["applied"] is True
    cmp_ = a["analysis"]["lowlight_comparison"]
    assert "original_counts" in cmp_ and "enhanced_counts" in cmp_
    assert client.get(f"/api/alerts/{a['id']}/image/comparison").status_code == 200
    assert client.get(f"/api/alerts/{a['id']}/image/enhanced").status_code == 200


@needs_weights
def test_real_yolo_video_tracking_and_annotation(client, sample_image, tmp_path):
    """Synthesises a 6 s video from the real photo with a slow drift, so tracks must persist across frames."""
    img = cv2.imread(str(sample_image))
    h, w = img.shape[:2]
    path = tmp_path / "clip.mp4"
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (w, h))
    for i in range(60):
        M = np.float32([[1, 0, i * 0.5], [0, 1, 0]])
        vw.write(cv2.warpAffine(img, M, (w, h)))
    vw.release()
    with open(path, "rb") as f:
        r = client.post("/api/upload", data={"file": (f, "clip.mp4"), "async": "1"}, content_type="multipart/form-data")
    assert r.status_code == 202
    jid = r.get_json()["job_id"]
    for _ in range(300):
        j = client.get(f"/api/jobs/{jid}").get_json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.5)
    assert j["status"] == "done", j
    a = j["result"]
    persons = [t for t in a["track_behaviors"] if t["class_name"] == "person"]
    assert 2 <= len(persons) <= 4                                  # 3 people -> ~3 unique tracks, not 1 per frame
    assert a["detections_total"] > len(persons) * 3                # many boxes, few identities
    assert all(t["dwell_seconds"] <= 6.5 for t in persons)         # dwell computed from real fps
    assert a["analysis"]["video"]["frames_analyzed"] >= 10
    # annotated frame only holds boxes of that single frame
    assert client.get(f"/api/alerts/{a['id']}/image/annotated").status_code == 200


@needs_weights
def test_real_yolo_empty_and_bad_video(client, tmp_path):
    p = tmp_path / "bad.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200)
    r = client.post("/api/upload", data={"file": (open(p, "rb"), "bad.mp4")}, content_type="multipart/form-data")
    assert r.status_code == 422
