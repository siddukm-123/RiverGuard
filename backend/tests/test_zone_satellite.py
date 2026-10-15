import pytest
from detection import zone_checker as Z
from satellite import gee_client as G, scoring as S

PTS = [{"lat": 14.40, "lng": 75.90}, {"lat": 14.46, "lng": 75.92}, {"lat": 14.52, "lng": 75.95}]
DAY = 86400000


def test_utm_zone_is_computed_not_hardcoded():
    assert Z.utm_epsg_for(14.46, 75.92) == 32643   # Davangere (old code used 44N)
    assert Z.utm_epsg_for(26.14, 91.74) == 32646   # Guwahati
    assert Z.utm_epsg_for(10.96, 78.07) == 32644   # Karur


def test_buffer_polygon_and_point_in_polygon():
    poly = Z.buffer_from_monitor_points(PTS, 1000)
    assert poly and poly["type"] == "Polygon"
    assert Z.point_in_buffer(14.46, 75.925, poly) is True
    assert Z.point_in_buffer(14.46, 75.95, poly) is False
    assert Z.point_in_buffer(None, None, poly) is None   # unknown is not False
    assert Z.point_in_buffer(14.46, 75.92, None) is None


def test_buffer_distance_is_about_the_requested_metres():
    poly = Z.buffer_from_monitor_points([{"lat": 14.40, "lng": 75.90}, {"lat": 14.50, "lng": 75.90}], 1000)
    lat = 14.45
    assert Z.point_in_buffer(lat, 75.90 + 900 / 107800, poly) is True   # ~900 m east of the line
    assert Z.point_in_buffer(lat, 75.90 + 1100 / 107800, poly) is False  # ~1100 m east


def test_polyline_distance_beats_centre_point_for_long_rivers():
    # a point 5 km along the river is far from the centre point but on the corridor
    pts = [{"lat": 14.40, "lng": 75.90}, {"lat": 14.60, "lng": 75.90}]
    assert Z.polyline_distance_check(14.55, 75.9005, pts, 1000) is True
    assert Z.simple_distance_check(14.55, 75.9005, 14.50, 75.90, 1000) is False


def test_scene_pair_rejects_same_scene_regression():
    """REGRESSION (paper Table II): same scene was used as both 'before' and 'after'."""
    s = {"id": "A", "time_ms": 1000 * DAY, "valid": 0.9}
    ok, why = G.check_pair(s, s, 10)
    assert not ok and "same scene" in why


def test_scene_pair_rejects_too_close_and_accepts_baseline():
    a = {"id": "A", "time_ms": 1000 * DAY, "valid": 0.9}
    near = {"id": "B", "time_ms": 995 * DAY, "valid": 0.9}
    far = {"id": "C", "time_ms": 970 * DAY, "valid": 0.9}
    assert not G.check_pair(near, a, 10)[0]
    assert G.check_pair(far, a, 10)[0]


def test_scene_choice_respects_clean_fraction_and_target():
    cands = [{"id": "x", "time_ms": 100 * DAY, "valid": 0.2}, {"id": "y", "time_ms": 103 * DAY, "valid": 0.8}, {"id": "z", "time_ms": 101 * DAY, "valid": 0.9}]
    assert G.choose_after(cands, 0.6)["id"] == "y"                    # newest clean scene
    assert G.choose_before(cands, 100 * DAY, 0.6)["id"] == "z"        # closest clean scene; 'x' too cloudy
    assert G.choose_after([{"id": "c", "time_ms": 1, "valid": 0.1}], 0.6) is None


def test_corridor_background_removed():
    pts = [{"status": "ok", "changed_fraction": f} for f in (0.30, 0.31, 0.29, 0.30)]  # whole river changed (e.g. flood)
    S.corridor_scores(pts)
    assert all(p["change_score"] < 0.1 for p in pts)  # river-wide change is removed (tiny residual only)


def test_corridor_hotspot_detected():
    pts = [{"status": "ok", "changed_fraction": f} for f in (0.05, 0.06, 0.05, 0.45)]
    S.corridor_scores(pts)
    assert S.pick_hotspot(pts)["changed_fraction"] == 0.45 and pts[3]["change_score"] == 1.0


def test_no_valid_points_means_no_hotspot():
    pts = [{"status": "no_scene"}, {"status": "error"}]
    S.corridor_scores(pts)
    assert S.pick_hotspot(pts) is None and pts[0]["change_score"] is None


def test_synthetic_only_without_credentials_and_flagged():
    r = G.get_before_after_pair(14.4, 75.9, None, "2026-08-01")
    assert r["synthetic"] is True and "SYNTHETIC" in r["message"]


def test_configured_gee_failure_never_fabricates(monkeypatch):
    import config
    monkeypatch.setattr(config, "GEE_SERVICE_ACCOUNT", "sa@proj.iam.gserviceaccount.com")
    monkeypatch.setattr(config, "GEE_PRIVATE_KEY_PATH", "/nonexistent.json")
    monkeypatch.setattr(G, "_gee_initialized", False)
    r = G.get_before_after_pair(14.4, 75.9, None, "2026-08-01")
    assert r["status"] == "error" and not r["synthetic"] and "change_score" not in r
