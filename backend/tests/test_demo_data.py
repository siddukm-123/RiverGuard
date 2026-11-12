"""TEST river + HIGH alert WITH PROOF: honest labelling, real scorer, real geometry, evidence image + hashed PDF, idempotent, removable."""
import hashlib
import re
import zlib
from pathlib import Path

import cv2
import pytest

import config
import demo_data
import gcs_storage
import scheduler
from api import routes
from api.supabase_client import table_delete, table_delete_where, table_insert, table_select

POINTS = [{"lat": 14.50 + i * 0.018, "lng": 75.80 + i * 0.004, "label": f"river segment {i + 1} (flow direction)"} for i in range(5)]


@pytest.fixture()
def osm(monkeypatch):
    """No real river with a corridor exists -> the test river is mapped from (mocked) OpenStreetMap."""
    calls = []

    def fake_snap(lat, lng, river_name=None, **kw):
        calls.append((lat, lng, river_name))
        return 14.5126, 75.805, [dict(p) for p in POINTS]
    monkeypatch.setattr("satellite.river_corridor.snap_to_river_center", fake_snap)
    monkeypatch.setattr(demo_data, "_pick_real_river", lambda: None)
    monkeypatch.setattr(demo_data.time, "sleep", lambda s: None)
    yield calls
    demo_data.remove()


INTEGER_COLUMNS = {"rivers": ("buffer_radius_m", "scan_interval_days"), "detections": ("bbox_x_min", "bbox_y_min", "bbox_x_max", "bbox_y_max")}


@pytest.fixture()
def strict_db(monkeypatch):
    """Rejects 1000.0 for integer columns exactly like Postgres ('invalid input syntax for type integer: 1000.0')."""
    def wrap(real):
        def insert(table, row):
            for col in INTEGER_COLUMNS.get(table, ()):
                if row.get(col) is not None and not isinstance(row[col], int):
                    raise Exception(f"{{'code': '22P02', 'message': 'invalid input syntax for type integer: {row[col]}'}}")
            return real(table, row)
        return insert
    monkeypatch.setattr(demo_data, "table_insert", wrap(demo_data.table_insert))
    monkeypatch.setattr(routes, "table_insert", wrap(routes.table_insert))


def test_integer_columns_get_integers_not_floats(client, osm, strict_db):
    """Regression: config.DEFAULT_BUFFER_RADIUS_M is the float 1000.0; sending it to the integer column caused 'Internal server error'."""
    r = client.post("/api/demo/seed")
    assert r.status_code == 201, r.get_json()
    assert isinstance(r.get_json()["river"]["buffer_radius_m"], int)


def _metrics(client):
    return client.get("/api/metrics").get_json()


def _pdf_text(path):
    raw = Path(path).read_bytes()
    out = b""
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        try:
            out += zlib.decompress(m.group(1))
        except zlib.error:
            out += m.group(1)
    return out


def test_seed_creates_labelled_test_river_with_real_scored_high_alert(client, osm):
    before = _metrics(client)
    r = client.post("/api/demo/seed")
    body = r.get_json()
    assert r.status_code == 201 and body["created"] is True and "warnings" not in body["alert"]

    river, alert = body["river"], body["alert"]
    assert river["name"] == "Tungabhadra River (Test)" and river["monitoring_enabled"] is False and len(river["monitor_points"]) == 5
    assert osm == [(14.5126, 75.805, "Tungabhadra River")]
    assert alert["severity"] == "HIGH" and alert["confidence"] >= 85
    assert alert["inside_buffer"] is True and alert["river_id"] == river["id"]
    assert (alert["lat"], alert["lng"]) == (POINTS[2]["lat"], POINTS[2]["lng"])
    assert alert["reasoning_text"].startswith("TEST ALERT")
    sig = alert["reasoning_signals"]
    assert sig["demo_test"] is True and sig["object_counts"] == {"truck": 2, "jcb": 1, "person": 3}
    assert sum(b["points"] for b in sig["score_breakdown"] if b["applied"]) == alert["confidence"]

    after = _metrics(client)
    assert after["high_severity"] == before["high_severity"] + 1 and after["rivers_monitored"] == before["rivers_monitored"] + 1
    assert after["rivers_scanned"] == before["rivers_scanned"]


def test_the_alert_comes_with_proof_image_detections_and_a_verifiable_pdf(client, osm):
    alert = client.post("/api/demo/seed").get_json()["alert"]
    aid = alert["id"]

    img_path = Path(alert["annotated_image_path"])                                   # evidence image
    img = cv2.imread(str(img_path))
    assert img is not None and img.shape[:2] == (720, 1280)
    api_img = client.get(f"/api/alerts/{aid}/annotated.jpg")
    assert api_img.status_code == 200 and api_img.mimetype == "image/jpeg" and api_img.data[:2] == b"\xff\xd8"

    dets = table_select("detections", filters={"alert_id": aid})                     # detection rows
    assert sorted(d["class_name"] for d in dets) == ["jcb", "person", "person", "person", "truck", "truck"]

    pdf = Path(alert["evidence_pdf_url"])                                            # evidence PDF + real hash
    assert pdf.exists() and pdf.read_bytes()[:5] == b"%PDF-"
    assert alert["evidence_sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert b"TEST ALERT" in _pdf_text(pdf) and b"NOT A REAL DETECTION" in _pdf_text(pdf)   # the PDF itself says it is a test
    v = client.get(f"/api/alerts/{aid}/evidence/verify").get_json()
    assert v["match"] is True


def test_the_dashboard_sees_the_proof_through_the_public_api(client, osm):
    aid = client.post("/api/demo/seed").get_json()["alert"]["id"]
    pub = next(a for a in client.get("/api/alerts").get_json() if a["id"] == aid)
    assert pub["evidence_pdf_url"] == f"/api/alerts/{aid}/evidence.pdf"
    assert pub["annotated_image_path"] == f"/api/alerts/{aid}/annotated.jpg"
    pdf = client.get(f"/api/alerts/{aid}/evidence.pdf")
    assert pdf.status_code == 200 and pdf.data[:5] == b"%PDF-"


def test_evidence_image_goes_to_the_bucket_when_gcs_is_on(client, osm, monkeypatch):
    store = {}

    class Blob:
        def __init__(self, name): self.name, self.content_type = name, None
        def upload_from_filename(self, f, content_type=None, timeout=None): store[self.name] = (Path(f).read_bytes(), content_type)
        def download_as_bytes(self, timeout=None):
            if self.name not in store:
                raise type("NotFound", (Exception,), {})()
            self.content_type = store[self.name][1]
            return store[self.name][0]

    monkeypatch.setattr(config, "GCS_BUCKET", "imagesmap")
    monkeypatch.setattr(gcs_storage, "_bucket", type("B", (), {"blob": lambda self, n: Blob(n)})())
    alert = client.post("/api/demo/seed").get_json()["alert"]
    aid = alert["id"]
    assert alert["annotated_image_path"] == f"gs://imagesmap/alerts/{aid}/annotated.jpg" and f"alerts/{aid}/annotated.jpg" in store
    assert client.get(f"/api/alerts/{aid}/annotated.jpg").data[:2] == b"\xff\xd8"


def test_geometry_is_copied_from_a_real_monitored_river_without_any_network_call(client, monkeypatch):
    calls = []
    monkeypatch.setattr("satellite.river_corridor.snap_to_river_center", lambda *a, **k: calls.append(a) or (0, 0, []))
    real = table_insert("rivers", {"name": "Tungabhadra River", "state": "Karnataka", "district": "Davanagere", "center_lat": 14.5, "center_lng": 75.8,
                                   "buffer_radius_m": 1000, "buffer_geojson": {"type": "Polygon", "coordinates": []},
                                   "monitor_points": [dict(p) for p in POINTS], "monitoring_enabled": True})
    try:
        body = client.post("/api/demo/seed").get_json()
        assert calls == []                                                           # instant: no OpenStreetMap call
        assert body["river"]["name"] == "Tungabhadra River (Test)" and body["river"]["monitor_points"] == POINTS
        assert body["river"]["id"] != real["id"] and (body["river"]["state"], body["river"]["district"]) == ("Karnataka", "Davanagere")
        assert body["alert"]["severity"] == "HIGH"
    finally:
        demo_data.remove()
        table_delete("rivers", real["id"])


def test_seed_twice_does_not_duplicate_and_keeps_the_same_proof(client, osm):
    first = client.post("/api/demo/seed").get_json()
    again = client.post("/api/demo/seed")
    assert again.status_code == 200 and again.get_json()["created"] is False and again.get_json()["alert"]["id"] == first["alert"]["id"]
    assert len(demo_data.demo_rivers()) == 1 and len(osm) == 1
    assert len([a for a in table_select("alerts") if (a.get("reasoning_signals") or {}).get("demo_test")]) == 1


def test_an_older_proofless_test_alert_is_replaced_not_duplicated(client, osm):
    from satellite.river_corridor import snap_to_river_center   # the (mocked) OpenStreetMap function
    river, _dropped = demo_data._create_river(snap_to_river_center, routes._build_buffer, 1000)
    old = table_insert("alerts", demo_data.build_alert(river, POINTS[2]))            # what the previous version created: no image, no PDF
    assert "evidence_pdf_url" not in old or not old.get("evidence_pdf_url")
    body = client.post("/api/demo/seed").get_json()
    assert body["created"] is True and body["alert"]["id"] != old["id"] and body["alert"]["evidence_pdf_url"]
    demo_alerts = [a for a in table_select("alerts") if (a.get("reasoning_signals") or {}).get("demo_test")]
    assert [a["id"] for a in demo_alerts] == [body["alert"]["id"]]


def test_remove_deletes_only_test_data_including_detections(client, osm):
    real_rivers = {r["id"] for r in table_select("rivers") if not demo_data.is_demo_river(r)}
    alerts_before = len(table_select("alerts"))
    aid = client.post("/api/demo/seed").get_json()["alert"]["id"]
    assert client.delete("/api/demo").get_json() == {"removed_rivers": 1}
    assert demo_data.demo_rivers() == [] and {r["id"] for r in table_select("rivers")} == real_rivers
    assert len(table_select("alerts")) == alerts_before and table_select("detections", filters={"alert_id": aid}) == []
    assert client.delete("/api/demo").get_json() == {"removed_rivers": 0}


def test_osm_failure_creates_nothing_and_says_why(client, monkeypatch):
    def down(*a, **k):
        raise TimeoutError("overpass busy")
    monkeypatch.setattr("satellite.river_corridor.snap_to_river_center", down)
    monkeypatch.setattr(demo_data, "_pick_real_river", lambda: None)
    monkeypatch.setattr(demo_data.time, "sleep", lambda s: None)
    r = client.post("/api/demo/seed")
    assert r.status_code == 503 and "OpenStreetMap" in r.get_json()["error"] and "TimeoutError" in r.get_json()["error"]
    assert demo_data.demo_rivers() == []


def test_a_database_failure_names_the_step_instead_of_a_bare_500_and_a_retry_recovers(client, osm, monkeypatch):
    real_insert = demo_data.table_insert

    def insert(table, row):
        if table == "alerts":
            raise RuntimeError("column foo does not exist")
        return real_insert(table, row)
    monkeypatch.setattr(demo_data, "table_insert", insert)
    r = client.post("/api/demo/seed")
    assert r.status_code == 503 and "while saving the alert" in r.get_json()["error"] and "column foo" in r.get_json()["error"]
    monkeypatch.setattr(demo_data, "table_insert", real_insert)                      # the river already exists -> retry just completes it
    again = client.post("/api/demo/seed")
    assert again.status_code == 201 and again.get_json()["alert"]["severity"] == "HIGH" and len(demo_data.demo_rivers()) == 1


def test_if_the_picture_fails_the_alert_is_still_created_with_a_warning(client, osm, monkeypatch):
    def boom(path):
        raise OSError("disk full")
    monkeypatch.setattr(demo_data, "render_scene", boom)
    body = client.post("/api/demo/seed").get_json()
    assert body["alert"]["severity"] == "HIGH" and any("Evidence image" in w for w in body["alert"]["warnings"])
    assert body["alert"]["evidence_pdf_url"]                                          # the PDF is still produced


def test_test_river_works_on_an_old_rivers_table_without_corridor_columns(client, osm, monkeypatch):
    """The most likely real-world cause of 'Internal server error': a database created from an older migration."""
    real_insert = demo_data.table_insert

    def insert(table, row):
        for col in ("monitor_points", "buffer_geojson"):
            if table == "rivers" and col in row:
                raise Exception(f"Could not find the '{col}' column of 'rivers' in the schema cache")
        return real_insert(table, row)
    monkeypatch.setattr(demo_data, "table_insert", insert)
    r = client.post("/api/demo/seed")
    body = r.get_json()
    assert r.status_code == 201 and body["alert"]["severity"] == "HIGH" and body["alert"]["evidence_pdf_url"]
    assert any("monitor_points" in w and "0001_init.sql" in w for w in body["warnings"])


def test_scheduler_never_scans_the_test_river(client, osm):
    river = client.post("/api/demo/seed").get_json()["river"]
    assert scheduler._due_for_scan({**river, "monitoring_enabled": True, "last_scanned_at": None}) is False
    assert scheduler._due_for_scan({"name": "Real River", "monitoring_enabled": True, "last_scanned_at": None}) is True