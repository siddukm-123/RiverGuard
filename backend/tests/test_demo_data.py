"""TEST river + HIGH alert: honest labelling, real scorer, real-channel geometry, idempotent, removable."""
import pytest

import demo_data
import scheduler
from api.supabase_client import table_select

# Five points ~2 km apart along a (fake) river channel, shaped like fetch_monitor_points() output.
POINTS = [{"lat": 14.50 + i * 0.018, "lng": 75.80 + i * 0.004, "label": f"river segment {i + 1} (flow direction)"} for i in range(5)]


@pytest.fixture()
def osm(monkeypatch):
    calls = []

    def fake_snap(lat, lng, river_name=None, **kw):
        calls.append((lat, lng, river_name))
        return 14.5126, 75.805, [dict(p) for p in POINTS]
    monkeypatch.setattr("satellite.river_corridor.snap_to_river_center", fake_snap)
    monkeypatch.setattr(demo_data.time, "sleep", lambda s: None)
    yield calls
    demo_data.remove()   # leave no test data behind for other tests


def _metrics(client):
    return client.get("/api/metrics").get_json()


def test_seed_creates_labelled_test_river_with_real_scored_high_alert(client, osm):
    before = _metrics(client)
    r = client.post("/api/demo/seed")
    body = r.get_json()
    assert r.status_code == 201 and body["created"] is True

    river, alert = body["river"], body["alert"]
    assert river["name"].endswith(" (Test)") and river["name"].startswith("Tungabhadra River")
    assert river["monitoring_enabled"] is False and len(river["monitor_points"]) == 5
    assert osm == [(14.5126, 75.805, "Tungabhadra River")]          # geometry requested from OpenStreetMap, not invented

    assert alert["severity"] == "HIGH" and alert["confidence"] >= 85  # high-confidence risk score
    assert alert["inside_buffer"] is True and alert["river_id"] == river["id"]
    assert (alert["lat"], alert["lng"]) == (POINTS[2]["lat"], POINTS[2]["lng"])   # exactly ON the river corridor
    assert alert["reasoning_text"].startswith("TEST ALERT")
    sig = alert["reasoning_signals"]
    assert sig["demo_test"] is True and sig["object_counts"] == {"truck": 2, "jcb": 1, "person": 3}
    assert sum(b["points"] for b in sig["score_breakdown"] if b["applied"]) == alert["confidence"]  # score comes from the real rules

    after = _metrics(client)
    assert after["high_severity"] == before["high_severity"] + 1 and after["rivers_monitored"] == before["rivers_monitored"] + 1
    assert after["rivers_scanned"] == before["rivers_scanned"]      # a TEST river is not counted as scanned


def test_seed_twice_does_not_duplicate(client, osm):
    client.post("/api/demo/seed")
    again = client.post("/api/demo/seed")
    assert again.status_code == 200 and again.get_json()["created"] is False
    assert len(demo_data.demo_rivers()) == 1
    assert len([a for a in table_select("alerts") if (a.get("reasoning_signals") or {}).get("demo_test")]) == 1
    assert len(osm) == 1                                           # OpenStreetMap asked only once


def test_remove_deletes_only_test_data(client, osm):
    real_before = {r["id"] for r in table_select("rivers") if not demo_data.is_demo_river(r)}
    alerts_before = len([a for a in table_select("alerts") if not (a.get("reasoning_signals") or {}).get("demo_test")])
    client.post("/api/demo/seed")
    out = client.delete("/api/demo").get_json()
    assert out == {"removed_rivers": 1}
    assert demo_data.demo_rivers() == []
    assert {r["id"] for r in table_select("rivers")} == real_before
    assert len([a for a in table_select("alerts")]) == alerts_before
    assert client.delete("/api/demo").get_json() == {"removed_rivers": 0}


def test_osm_failure_creates_nothing_and_says_why(client, monkeypatch):
    def down(*a, **k):
        raise TimeoutError("overpass busy")
    monkeypatch.setattr("satellite.river_corridor.snap_to_river_center", down)
    monkeypatch.setattr(demo_data.time, "sleep", lambda s: None)
    r = client.post("/api/demo/seed")
    assert r.status_code == 503 and "OpenStreetMap" in r.get_json()["error"] and "TimeoutError" in r.get_json()["error"]
    assert demo_data.demo_rivers() == []                           # no invented coordinates, nothing half-created


def test_scheduler_never_scans_the_test_river(client, osm):
    river = client.post("/api/demo/seed").get_json()["river"]
    assert scheduler._due_for_scan({**river, "monitoring_enabled": True, "last_scanned_at": None}) is False
    assert scheduler._due_for_scan({"name": "Real River", "monitoring_enabled": True, "last_scanned_at": None}) is True
