"""Uploads must survive bookkeeping failures, 500s must say what failed, and videos must return per-frame boxes."""
import io
import time

import cv2
import numpy as np
import pytest

import pipeline
from api import routes
from detection import detector

TRUCK = {"class_name": "truck", "raw_class": "truck", "confidence": 0.91, "bbox": (10.0, 12.0, 60.0, 44.0),
         "model": "base", "role": "evidence", "view": "original"}


@pytest.fixture()
def stub_model(monkeypatch):
    """A 'detector' that always sees one truck - no YOLO weights needed."""
    def fake(frame, mode="off", conf=None):
        return {"detections": [dict(TRUCK)], "original": [dict(TRUCK)], "enhanced": None, "enhanced_frame": None,
                "lowlight": {"mode": mode, "applied": False, "reason": "stub", "enhancement": None,
                             "assessment": {"mean_luma": 90.0, "noise_sigma": 1.0, "blur_laplacian_var": 50.0,
                                            "dark_pixel_fraction": 0.0}}}
    monkeypatch.setattr(detector, "detect_frame_lowlight", fake)
    monkeypatch.setattr(detector, "get_model_info", lambda: {"name": "stub", "excavator_detection_available": False})


def _png_bytes():
    ok, buf = cv2.imencode(".png", np.full((120, 160, 3), 120, np.uint8))
    return io.BytesIO(buf.tobytes())


def _mp4(tmp_path):
    path = tmp_path / "clip.mp4"
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (160, 120))
    for i in range(40):  # 4 s
        w.write(np.full((120, 160, 3), 60 + i, np.uint8))
    w.release()
    return path


def _post_image(client, **extra):
    return client.post("/api/upload", data={"file": (_png_bytes(), "scene.png"), **extra}, content_type="multipart/form-data")


def test_image_upload_works_when_the_uploads_log_table_is_broken(client, stub_model, monkeypatch):
    real_insert, real_update = routes.table_insert, routes.table_update

    def insert(table, row):
        if table == "uploads":
            raise RuntimeError("uploads table unavailable")
        return real_insert(table, row)

    def update(table, row_id, fields):
        if table == "uploads":
            raise RuntimeError("uploads table unavailable")
        return real_update(table, row_id, fields)
    monkeypatch.setattr(routes, "table_insert", insert)
    monkeypatch.setattr(routes, "table_update", update)
    r = _post_image(client)
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["object_counts"] == {"truck": 1}


def test_pipeline_error_message_names_the_real_cause(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("operator torchvision::nms does not exist")
    monkeypatch.setattr(pipeline, "process_image_upload", boom)
    r = _post_image(client)
    body = r.get_json()
    assert r.status_code == 500 and "torchvision::nms" in body["error"]


def test_unhandled_500_carries_reference_and_reason(client, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("database unreachable")
    monkeypatch.setattr(routes, "table_select", boom)
    r = _post_image(client, river_id="some-river")
    body = r.get_json()
    assert r.status_code == 500 and body["code"] == "internal_error"
    assert len(body["ref"]) == 8 and "ConnectionError" in body["detail"] and "database unreachable" in body["detail"]
    assert "Traceback" not in str(body)


def test_video_upload_returns_boxes_for_every_analysed_frame(client, stub_model, tmp_path):
    clip = _mp4(tmp_path)
    with open(clip, "rb") as f:
        r = client.post("/api/upload", data={"file": (f, "clip.mp4"), "async": "1"}, content_type="multipart/form-data")
    assert r.status_code == 202
    jid = r.get_json()["job_id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{jid}").get_json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["status"] == "done", job
    ov = job["result"]["video_overlay"]
    assert (ov["width"], ov["height"]) == (160, 120)
    assert len(ov["frames"]) >= 4 and ov["truncated"] is False
    first = ov["frames"][0]
    assert first["boxes"][0][:7] == [10.0, 12.0, 60.0, 44.0, "truck", 0.91, first["boxes"][0][6]]
    assert all(a["t"] < b["t"] for a, b in zip(ov["frames"], ov["frames"][1:]))  # time ordered for playback sync
    assert job["result"]["object_counts"] == {"truck": 1}   # one tracked truck, not one per frame


def test_overlay_is_capped_and_says_so():
    dets = [{"bbox": (0, 0, 1, 1), "class_name": "truck", "confidence": 0.5, "timestamp_s": i * 0.5, "role": "evidence"}
            for i in range(50)]
    ov = pipeline.build_video_overlay(dets, {"frame_width": 10, "frame_height": 10, "sample_fps": 2}, max_boxes=20)
    assert sum(len(f["boxes"]) for f in ov["frames"]) == 20 and ov["truncated"] is True


def test_system_status_has_library_versions_and_model_probe(client, stub_model, monkeypatch):
    monkeypatch.setattr(detector, "detect_frame", lambda frame, conf=None: [])
    st = client.get("/api/system-status?probe=1").get_json()
    assert st["model_probe"]["ok"] is True
    assert set(st["libraries"]) >= {"torch", "torchvision", "ultralytics", "numpy"}


def test_model_probe_reports_failure_instead_of_raising(monkeypatch):
    def boom(frame, conf=None):
        raise RuntimeError("weights corrupt")
    monkeypatch.setattr(detector, "detect_frame", boom)
    res = detector.probe()
    assert res["ok"] is False and "weights corrupt" in res["error"]
