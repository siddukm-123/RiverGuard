"""Manual detections are saved with a name, their video and boxes, and listed for the Details page."""
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import pytest

import config
import gcs_storage
import pipeline
import saved_detections
from api import routes
from api.supabase_client import table_insert, table_select
from detection import detector

VIDEO = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + bytes(range(256)) * 20      # 5 KB, looks like an mp4
OVERLAY = {"width": 160, "height": 120, "sample_fps": 2, "truncated": False,
           "frames": [{"t": 0.0, "boxes": [[1, 2, 30, 40, "truck", 0.9, 1, "evidence"]]}, {"t": 0.5, "boxes": [[5, 2, 34, 40, "truck", 0.9, 1, "evidence"]]}]}


@pytest.fixture()
def fake_video_pipeline(monkeypatch):
    def fake(path, river=None, lat=None, lng=None, lowlight_mode="off", original_name=None):
        alert = table_insert("alerts", {"source": "manual_upload", "severity": "WATCH", "confidence": 40, "reasoning_text": "Assessed as WATCH.",
                                        "detected_at": datetime.now(timezone.utc).isoformat(),
                                        "reasoning_signals": {"input_type": "video", "object_counts": {"truck": 1, "person": 2},
                                                              "media": {"original": str(path)}, "video": {"duration_s": 4.0, "frames_analyzed": 8}}})
        return {**alert, "video_overlay": OVERLAY, "object_counts": {"truck": 1, "person": 2}}
    monkeypatch.setattr(pipeline, "process_video_upload", fake)


class NotFound(Exception):
    pass


class Blob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    @property
    def size(self):
        return len(self.store[self.name][0])

    @property
    def content_type(self):
        return self.store[self.name][1]

    def upload_from_filename(self, f, content_type=None, timeout=None):
        self.store[self.name] = (Path(f).read_bytes(), content_type)

    def upload_from_string(self, data, content_type=None, timeout=None):
        self.store[self.name] = (data, content_type)

    def download_as_bytes(self, start=None, end=None, timeout=None):
        if self.name not in self.store:
            raise NotFound(self.name)
        data = self.store[self.name][0]
        return data if start is None else data[start:(end + 1 if end is not None else None)]

    def delete(self):
        self.store.pop(self.name, None)


class Bucket:
    def __init__(self):
        self.store = {}

    def blob(self, name):
        return Blob(self.store, name)

    def get_blob(self, name):
        return Blob(self.store, name) if name in self.store else None


@pytest.fixture()
def bucket(monkeypatch):
    b = Bucket()
    monkeypatch.setattr(config, "GCS_BUCKET", "imagesmap")
    monkeypatch.setattr(gcs_storage, "_bucket", b)
    return b


def upload_video(client, name="Harihar drone flight 1", filename="drone_flight.mp4"):
    data = {"file": (io.BytesIO(VIDEO), filename)}
    if name is not None:
        data["name"] = name
    return client.post("/api/upload", data=data, content_type="multipart/form-data")


def entry(client, alert_id):
    return next(d for d in client.get("/api/detections").get_json() if d["id"] == alert_id)


def test_video_is_saved_with_the_users_name_video_and_boxes(client, fake_video_pipeline):
    r = upload_video(client)
    body = r.get_json()
    assert r.status_code == 201 and body["saved"]["name"] == "Harihar drone flight 1" and body["saved"]["video_stored"] is True
    assert any("No storage bucket" in w for w in body["saved"]["warnings"])               # honest: local disk is lost at a redeploy
    d = entry(client, body["id"])
    assert (d["name"], d["file_kind"], d["original_filename"]) == ("Harihar drone flight 1", "video", "drone_flight.mp4")
    assert d["has_video"] and d["video_url"] == f"/api/alerts/{body['id']}/video" and d["overlay_url"] == f"/api/alerts/{body['id']}/overlay"
    assert d["object_counts"] == {"truck": 1, "person": 2} and d["severity"] == "WATCH" and d["video_info"]["duration_s"] == 4.0
    v = client.get(d["video_url"])
    assert v.status_code == 200 and v.data == VIDEO and v.headers["Accept-Ranges"] == "bytes"
    part = client.get(d["video_url"], headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.data == VIDEO[10:20]                          # seeking works on a local file
    assert client.get(d["overlay_url"]).get_json() == OVERLAY


def test_without_a_name_the_file_name_is_used(client, fake_video_pipeline):
    body = upload_video(client, name=None, filename="drone_flight.mp4").get_json()
    assert body["saved"]["name"] == "drone_flight" and entry(client, body["id"])["name"] == "drone_flight"
    assert saved_detections.clean_name("  a   b  ", "x.mp4") == "a b" and saved_detections.clean_name("", "My  clip.MOV") == "My clip"
    assert len(saved_detections.clean_name("x" * 500)) == 120


def test_video_and_boxes_go_to_the_bucket_and_stream_with_range_requests(client, fake_video_pipeline, bucket, monkeypatch):
    body = upload_video(client).get_json()
    aid = body["id"]
    assert body["saved"]["warnings"] == [] and bucket.store[f"alerts/{aid}/video.mp4"][0] == VIDEO
    assert json.loads(bucket.store[f"alerts/{aid}/overlay.json"][0]) == OVERLAY
    url, size = f"/api/alerts/{aid}/video", len(VIDEO)

    whole = client.get(url)
    assert whole.status_code == 200 and whole.data == VIDEO and whole.headers["Content-Length"] == str(size)
    p = client.get(url, headers={"Range": "bytes=5-14"})
    assert p.status_code == 206 and p.data == VIDEO[5:15] and p.headers["Content-Range"] == f"bytes 5-14/{size}"
    assert client.get(url, headers={"Range": "bytes=-10"}).data == VIDEO[-10:]            # last 10 bytes
    assert client.get(url, headers={"Range": f"bytes={size - 4}-"}).data == VIDEO[-4:]    # open ended
    bad = client.get(url, headers={"Range": f"bytes={size + 5}-"})
    assert bad.status_code == 416 and bad.headers["Content-Range"] == f"bytes */{size}"
    monkeypatch.setattr(routes, "VIDEO_CHUNK_BYTES", 1000)                                 # big videos are answered in chunks
    chunk = client.get(url, headers={"Range": "bytes=0-"})
    assert chunk.status_code == 206 and len(chunk.data) == 1000 and chunk.headers["Content-Range"] == f"bytes 0-999/{size}"
    assert client.get(f"/api/alerts/{aid}/overlay").get_json() == OVERLAY


def test_a_failed_bucket_upload_keeps_the_video_on_disk_with_a_warning(client, fake_video_pipeline, bucket, monkeypatch):
    monkeypatch.setattr(gcs_storage, "upload_file", lambda *a, **k: None)
    body = upload_video(client).get_json()
    assert body["saved"]["video_stored"] is True and any("could not be copied to cloud storage" in w for w in body["saved"]["warnings"])
    assert client.get(f"/api/alerts/{body['id']}/video").data == VIDEO


def test_saving_can_never_fail_a_finished_detection(client, fake_video_pipeline, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk exploded")
    monkeypatch.setattr(saved_detections, "persist", boom)
    r = upload_video(client)
    assert r.status_code == 201 and r.get_json()["severity"] == "WATCH" and any("Could not save" in w for w in r.get_json()["saved"]["warnings"])


def test_image_upload_is_saved_with_its_name_too(client, monkeypatch):
    def fake(frame, mode="off", conf=None):
        det = {"class_name": "truck", "raw_class": "truck", "confidence": 0.9, "bbox": (10.0, 12.0, 60.0, 44.0), "model": "base", "role": "evidence", "view": "original"}
        return {"detections": [dict(det)], "original": [dict(det)], "enhanced": None, "enhanced_frame": None,
                "lowlight": {"mode": mode, "applied": False, "reason": "stub", "enhancement": None,
                             "assessment": {"mean_luma": 90.0, "noise_sigma": 1.0, "blur_laplacian_var": 50.0, "dark_pixel_fraction": 0.0}}}
    monkeypatch.setattr(detector, "detect_frame_lowlight", fake)
    monkeypatch.setattr(detector, "get_model_info", lambda: {"name": "stub", "excavator_detection_available": False})
    ok, buf = cv2.imencode(".png", np.full((120, 160, 3), 120, np.uint8))
    r = client.post("/api/upload", data={"file": (io.BytesIO(buf.tobytes()), "scene.png"), "name": "Sand heap near bridge"}, content_type="multipart/form-data")
    assert r.status_code == 201 and r.get_json()["saved"]["video_stored"] is False
    d = entry(client, r.get_json()["id"])
    assert d["name"] == "Sand heap near bridge" and d["file_kind"] == "image" and d["has_video"] is False and d["video_url"] is None
    assert d["image_url"].endswith("/annotated.jpg") and d["pdf_url"] and d["object_counts"] == {"truck": 1}
    assert client.get(d["image_url"]).status_code == 200


def test_rename_and_its_validation(client, fake_video_pipeline):
    aid = upload_video(client).get_json()["id"]
    assert client.patch(f"/api/detections/{aid}", json={"name": "  Renamed   clip "}).get_json()["name"] == "Renamed clip"
    assert entry(client, aid)["name"] == "Renamed clip"
    r = client.patch(f"/api/detections/{aid}", json={"name": "   "})
    assert r.status_code == 400 and "name" in r.get_json()["error"].lower()
    sat = table_insert("alerts", {"source": "satellite", "severity": "WATCH", "confidence": 40, "detected_at": datetime.now(timezone.utc).isoformat()})
    assert client.patch(f"/api/detections/{sat['id']}", json={"name": "x"}).status_code == 404       # only manual detections


def test_delete_removes_everything_and_never_touches_satellite_alerts(client, fake_video_pipeline, bucket):
    aid = upload_video(client).get_json()["id"]
    table_insert("detections", {"alert_id": aid, "class_name": "truck", "confidence": 0.9})
    assert f"alerts/{aid}/video.mp4" in bucket.store
    assert client.delete(f"/api/detections/{aid}").get_json() == {"deleted": aid}
    assert not any(k.startswith(f"alerts/{aid}/") for k in bucket.store)                   # video, boxes, pictures, pdf
    assert table_select("alerts", filters={"id": aid}) == [] and table_select("detections", filters={"alert_id": aid}) == []
    assert all(d["id"] != aid for d in client.get("/api/detections").get_json())
    assert client.get(f"/api/alerts/{aid}/video").status_code == 404

    sat = table_insert("alerts", {"source": "satellite", "severity": "WATCH", "confidence": 40, "detected_at": datetime.now(timezone.utc).isoformat()})
    assert client.delete(f"/api/detections/{sat['id']}").status_code == 404
    assert table_select("alerts", filters={"id": sat["id"]})                               # still there


def test_local_files_are_only_deleted_inside_our_own_folders(tmp_path):
    outside = tmp_path / "precious.txt"
    outside.write_text("keep me")
    routes._remove_file(str(outside))
    assert outside.exists()
    inside = Path(config.EVIDENCE_DIR) / "x_overlay.json"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("{}")
    routes._remove_file(str(inside))
    assert not inside.exists()


def test_missing_media_gives_clean_404s(client, fake_video_pipeline):
    aid = upload_video(client).get_json()["id"]
    Path(next(d for d in client.get("/api/detections").get_json() if d["id"] == aid) and
         table_select("alerts", filters={"id": aid})[0]["reasoning_signals"]["media"]["video"]).unlink()
    r = client.get(f"/api/alerts/{aid}/video")
    assert r.status_code == 404 and "no longer on the server" in r.get_json()["error"]
    assert client.get("/api/alerts/nope/video").status_code == 404
