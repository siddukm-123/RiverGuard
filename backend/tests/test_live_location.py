"""Live location: Google Maps link -> coordinates -> real place name -> rivers inside the radius -> saved monitored area."""
import pytest
import requests

from api import routes
from api.supabase_client import table_select
from detection import zone_checker
from satellite import live_location as ll

CENTER = (14.5126, 75.8050)


def line(a, b, n=40):
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def way(wid, name, coords, waterway="river"):
    tags = {"waterway": waterway}
    if name:
        tags["name"] = name
    return {"type": "way", "id": wid, "tags": tags, "geometry": [{"lat": la, "lon": lo} for la, lo in coords]}


# Tungabhadra: ~1 km from the centre, running diagonally, mapped as TWO connected ways (must be merged).
TUNGA = line((14.43, 75.74), (14.60, 75.90), 60)
WAYS = [
    way(1, "Tungabhadra", TUNGA[:31]), way(2, "Tungabhadra", TUNGA[30:]),
    way(3, "Haridra", line((14.46, 75.70), (14.46, 75.95), 40)),               # ~5.8 km south of the centre
    way(4, None, line((14.5126, 75.8100), (14.5130, 75.8110), 3)),             # tiny unnamed stub (< 500 m)
    way(5, "Far Canal", line((14.90, 75.70), (14.90, 75.95), 40), "canal"),    # ~43 km away
]


# ── parsing ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("14.4644, 75.9218", (14.4644, 75.9218)),
    ("14.4644 75.9218", (14.4644, 75.9218)),
    ("-33.8688, 151.2093", (-33.8688, 151.2093)),
    ("14.4644°N 75.9218°E", (14.4644, 75.9218)),
    ("https://www.google.com/maps/@14.4644,75.9218,15z", (14.4644, 75.9218)),
    ("https://maps.google.com/?q=14.4644,75.9218&z=15", (14.4644, 75.9218)),
    ("https://www.google.com/maps/search/14.4644,+75.9218", (14.4644, 75.9218)),
    ("https://www.google.com/maps/dir//14.4644,75.9218/", (14.4644, 75.9218)),
    # a place link: the exact pin (!3d!4d) wins over the map centre (@)
    ("https://www.google.com/maps/place/Harihar,+Karnataka/@14.5000,75.8000,13z/data=!3m1!4b1!4m6!3m5!1s0x3bbb:0x1!8m2!3d14.5126!4d75.805", (14.5126, 75.805)),
])
def test_coordinates_from_text_and_google_links(text, expected):
    assert ll.parse_location(text)["lat"] == pytest.approx(expected[0], abs=1e-6)
    assert ll.parse_location(text)["lng"] == pytest.approx(expected[1], abs=1e-6)


def test_degrees_minutes_seconds_like_google_shows_them():
    got = ll.parse_location("14°27'51.6\"N 75°55'18.5\"E")
    assert (got["lat"], got["lng"]) == (pytest.approx(14.464333, abs=1e-5), pytest.approx(75.921806, abs=1e-5))


@pytest.mark.parametrize("bad", ["", "   ", "hello world", "Tower 12, 34th Street", "123.4, 200.5", "0.0, 0.0", "x" * 3000])
def test_unusable_input_gets_a_friendly_error(bad):
    with pytest.raises(ll.LocationError) as e:
        ll.parse_location(bad)
    assert str(e.value) and "Traceback" not in str(e.value)


def test_non_google_links_are_refused_without_any_request():
    calls = []
    for url in ("http://169.254.169.254/latest/meta-data/", "https://evil.example.com/maps/@14.4,75.9,15z", "ftp://x/14.4,75.9"):
        with pytest.raises(ll.LocationError):
            ll.parse_location(url, getter=lambda u: calls.append(u))
    assert calls == []                                                          # the server never contacted them


class Resp:
    def __init__(self, status=302, location=None):
        self.status_code, self.headers = status, ({"Location": location} if location else {})


def test_short_link_is_resolved_through_google_only():
    seen = []

    def getter(url):
        seen.append(url)
        return Resp(302, "https://www.google.com/maps/place/X/@14.5,75.8,17z/data=!3d14.5126!4d75.805")
    out = ll.parse_location("https://maps.app.goo.gl/AbCdEf", getter=getter)
    assert (out["lat"], out["lng"], out["source"]) == (14.5126, 75.805, "google_short_link")
    assert seen == ["https://maps.app.goo.gl/AbCdEf"]


def test_short_link_redirecting_away_from_google_is_refused():
    seen = []

    def getter(url):
        seen.append(url)
        return Resp(302, "http://10.0.0.5/admin")
    with pytest.raises(ll.LocationError):
        ll.parse_location("https://maps.app.goo.gl/AbCdEf", getter=getter)
    assert seen == ["https://maps.app.goo.gl/AbCdEf"]                           # the evil target was never requested


def test_short_link_with_cookie_consent_wall_and_redirect_loops_and_network_errors():
    consent = "https://consent.google.com/m?continue=https%3A%2F%2Fwww.google.com%2Fmaps%2F%4014.5%2C75.8%2C15z&gl=DE"
    assert ll.parse_location("https://maps.app.goo.gl/x", getter=lambda u: Resp(302, consent))["lat"] == 14.5
    with pytest.raises(ll.LocationError, match="too many"):
        ll.parse_location("https://maps.app.goo.gl/x", getter=lambda u: Resp(302, "https://maps.app.goo.gl/x"))

    def down(u):
        raise requests.ConnectionError("no route")
    with pytest.raises(ll.LocationError, match="short link"):
        ll.parse_location("https://maps.app.goo.gl/x", getter=down)


# ── place name ────────────────────────────────────────────────────────────
class Fake:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_reverse_geocode_builds_the_correct_place_name_and_caches():
    calls = []

    def get(url, **kw):
        calls.append(kw["params"])
        return Fake({"display_name": "Harihar, Davanagere, Karnataka, India",
                     "address": {"town": "Harihar", "county": "Davanagere", "state": "Karnataka", "country": "India", "country_code": "in"}})
    place = ll.reverse_geocode(14.51261, 75.80501, http_get=get)
    assert place["place_name"] == "Harihar, Davanagere district, Karnataka, India"
    assert (place["locality"], place["district"], place["state"]) == ("Harihar", "Davanagere", "Karnataka")
    assert ll.reverse_geocode(14.51262, 75.80502, http_get=get) == place and len(calls) == 1


def test_reverse_geocode_failure_returns_none():
    def boom(url, **kw):
        raise requests.Timeout("slow")
    assert ll.reverse_geocode(1.2345, 2.3456, http_get=boom) is None


# ── rivers inside the radius ──────────────────────────────────────────────
def test_only_rivers_inside_the_radius_nearest_first():
    near = ll.find_nearby_rivers(*CENTER, radius_km=5, fetch=lambda *a: WAYS)
    assert [r["name"] for r in near] == ["Tungabhadra"]                         # Haridra is ~5.8 km away, canal ~43 km, stub too short
    wide = ll.find_nearby_rivers(*CENTER, radius_km=10, fetch=lambda *a: WAYS)
    assert [r["name"] for r in wide] == ["Tungabhadra", "Haridra"]              # wider radius finds more, nearest first
    assert wide[0]["distance_km"] < wide[1]["distance_km"]


def test_points_lie_on_the_channel_inside_the_radius_and_are_labelled():
    rivers = ll.find_nearby_rivers(*CENTER, radius_km=10, fetch=lambda *a: WAYS)
    total = 0
    for r in rivers:
        assert len(r["points"]) >= 2
        for p in r["points"]:
            total += 1
            assert ll.rc._haversine_m(*CENTER, p["lat"], p["lng"]) <= 10_000 + 50           # inside the radius
            assert p["river"] == r["name"] and r["name"] in p["label"] and p["on_river"] is True
    assert total <= 12                                                                       # satellite scans use up to ~10-12 points
    tunga = next(r for r in rivers if r["name"] == "Tungabhadra")
    assert max(ll._dist_point_to_polyline_m(p["lat"], p["lng"], TUNGA) for p in tunga["points"]) < 60   # on the real channel


def test_river_is_clipped_to_the_radius_not_taken_whole():
    r = ll.find_nearby_rivers(*CENTER, radius_km=3, fetch=lambda *a: WAYS)[0]
    assert 4 < r["length_km"] < 7                                                # ~2 * sqrt(3^2 - 1.2^2) km of a ~25 km river


def test_nothing_found_and_osm_errors():
    assert ll.find_nearby_rivers(*CENTER, radius_km=5, fetch=lambda *a: []) == []

    def down(*a):
        raise RuntimeError("OpenStreetMap river data is not reachable right now")
    with pytest.raises(RuntimeError):
        ll.find_nearby_rivers(*CENTER, radius_km=5, fetch=down)


def test_overpass_mirror_failover_stream_fallback_and_outage(monkeypatch):
    posted = []

    def post(url, data=None, **kw):
        posted.append((url, data["data"]))
        if "overpass-api.de" in url:
            raise requests.ConnectionError("mirror 1 down")
        elements = [] if "river|canal" in data["data"] else [way(9, "Halla", line((14.51, 75.80), (14.52, 75.82), 10), "stream")]
        return Fake({"elements": elements})
    monkeypatch.setattr(ll.requests, "post", post)
    ways = ll.fetch_waterways_around(*CENTER, 5000)
    assert [w["tags"]["name"] for w in ways] == ["Halla"]                        # no river -> named streams (small seasonal rivers)
    assert any("kumi" in u for u, _ in posted)                                   # second mirror used

    monkeypatch.setattr(ll.requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("all down")))
    with pytest.raises(RuntimeError, match="not reachable"):
        ll.fetch_waterways_around(*CENTER, 5000)


def test_preview_and_save_share_one_slow_osm_call(monkeypatch):
    calls = []
    monkeypatch.setattr(ll, "find_nearby_rivers", lambda *a, **k: calls.append(a) or [{"name": "R", "points": []}])
    ll._AREA_CACHE.clear()
    ll.find_nearby_rivers_cached(14.5, 75.8, 5)
    ll.find_nearby_rivers_cached(14.5, 75.8, 5)
    ll.find_nearby_rivers_cached(14.5, 75.8, 10)                                 # different radius -> new lookup
    assert len(calls) == 2


@pytest.mark.parametrize("v,ok", [(1, True), (5, True), ("15", True), (0.5, False), (16, False), ("abc", False), (None, False)])
def test_radius_limits(v, ok):
    if ok:
        assert ll.clamp_radius_km(v) == float(v)
    else:
        with pytest.raises(ll.LocationError):
            ll.clamp_radius_km(v)


# ── API ───────────────────────────────────────────────────────────────────
PLACE = {"place_name": "Harihar, Davanagere district, Karnataka, India", "locality": "Harihar", "district": "Davanagere",
         "state": "Karnataka", "country": "India", "display_name": "Harihar, ..."}
TWO_RIVERS = [
    {"name": "Tungabhadra", "distance_km": 1.2, "length_km": 6.0, "points": [
        {"lat": 14.50 + i * 0.01, "lng": 75.78, "label": f"Tungabhadra · point {i + 1}/4", "on_river": True, "river": "Tungabhadra"} for i in range(4)]},
    {"name": "Haridra", "distance_km": 4.0, "length_km": 5.0, "points": [
        {"lat": 14.50 + i * 0.01, "lng": 75.84, "label": f"Haridra · point {i + 1}/4", "on_river": True, "river": "Haridra"} for i in range(4)]},
]


@pytest.fixture()
def fake_world(monkeypatch):
    state = {"rivers": TWO_RIVERS}
    monkeypatch.setattr(ll, "reverse_geocode", lambda lat, lng, **k: PLACE)
    monkeypatch.setattr(ll, "find_nearby_rivers_cached", lambda lat, lng, r, **k: state["rivers"])
    yield state
    for r in [r for r in table_select("rivers") if r.get("area_kind") == "live_area"]:
        routes.table_delete_where("alerts", {"river_id": r["id"]})
        routes.table_delete("rivers", r["id"])


GOOGLE = "https://www.google.com/maps/place/Harihar/@14.5,75.8,13z/data=!3d14.5126!4d75.805"


def test_preview_shows_correct_place_name_and_rivers_and_saves_nothing(client, fake_world):
    before = len(table_select("rivers"))
    r = client.post("/api/live-location/preview", json={"input": GOOGLE, "radius_km": 5})
    body = r.get_json()
    assert r.status_code == 200 and (body["lat"], body["lng"]) == (14.5126, 75.805) and body["source"] == "google_maps"
    assert body["place_name"] == PLACE["place_name"] and body["radius_km"] == 5.0
    assert [x["name"] for x in body["rivers"]] == ["Tungabhadra", "Haridra"] and "message" not in body
    assert len(table_select("rivers")) == before


def test_preview_without_rivers_explains_and_suggests_a_wider_radius(client, fake_world):
    fake_world["rivers"] = []
    body = client.post("/api/live-location/preview", json={"input": "14.5126, 75.805", "radius_km": 3}).get_json()
    assert body["rivers"] == [] and "within 3 km" in body["message"] and "15 km" in body["message"]


def test_preview_rejects_bad_input_and_radius_with_friendly_messages(client, fake_world):
    r = client.post("/api/live-location/preview", json={"input": "hello"})
    assert r.status_code == 400 and r.get_json()["code"] == "invalid_location" and "coordinates" in r.get_json()["error"]
    r = client.post("/api/live-location/preview", json={"input": "14.5126, 75.805", "radius_km": 99})
    assert r.status_code == 400 and "between 1 and 15 km" in r.get_json()["error"]


def test_osm_outage_is_a_clear_503(client, monkeypatch):
    monkeypatch.setattr(ll, "reverse_geocode", lambda *a, **k: PLACE)

    def down(*a, **k):
        raise RuntimeError("OpenStreetMap river data is not reachable right now")
    monkeypatch.setattr(ll, "find_nearby_rivers_cached", down)
    r = client.post("/api/live-location/preview", json={"input": "14.5126, 75.805"})
    assert r.status_code == 503 and "try again" in r.get_json()["error"].lower()


def test_save_stores_users_own_name_and_the_correct_place_name(client, fake_world):
    before = client.get("/api/metrics").get_json()
    r = client.post("/api/live-location", json={"input": GOOGLE, "name": "  My   sand  point ", "radius_km": 5})
    body = r.get_json()
    assert r.status_code == 201 and body["warnings"] == []
    river = body["river"]
    assert river["name"] == "My sand point"                                      # the user's own name (whitespace tidied)
    assert river["place_name"] == PLACE["place_name"] and river["radius_km"] == 5.0 and river["area_kind"] == "live_area"
    assert river["river_names"] == "Tungabhadra, Haridra" and (river["state"], river["district"]) == ("Karnataka", "Davanagere")
    assert (river["center_lat"], river["center_lng"]) == (14.5126, 75.805)       # centred on the pasted spot
    assert len(river["monitor_points"]) == 8 and body["rivers"][0]["point_count"] == 4
    assert client.get("/api/metrics").get_json()["rivers_monitored"] == before["rivers_monitored"] + 1
    assert any(x["id"] == river["id"] and x["area_kind"] == "live_area" for x in client.get("/api/rivers").get_json())


def test_live_area_sends_an_integer_radius_to_the_integer_column(client, fake_world, monkeypatch):
    real = routes.table_insert

    def strict(table, row):
        assert table != "rivers" or isinstance(row["buffer_radius_m"], int), f"buffer_radius_m must be int, got {row['buffer_radius_m']!r}"
        return real(table, row)
    monkeypatch.setattr(routes, "table_insert", strict)
    assert client.post("/api/live-location", json={"input": GOOGLE}).status_code == 201


def test_save_without_a_name_uses_the_real_place_name(client, fake_world):
    assert client.post("/api/live-location", json={"lat": 14.5126, "lng": 75.805}).get_json()["river"]["name"] == "Harihar"


def test_two_rivers_are_not_joined_by_a_false_buffer(client, fake_world):
    river = client.post("/api/live-location", json={"input": GOOGLE}).get_json()["river"]
    geo = river["buffer_geojson"]
    assert zone_checker.point_in_buffer(14.51, 75.78, geo) is True               # on the Tungabhadra
    assert zone_checker.point_in_buffer(14.51, 75.84, geo) is True               # on the Haridra
    assert zone_checker.point_in_buffer(14.51, 75.81, geo) is False              # 3 km between them: no bridge
    pts = river["monitor_points"]
    assert zone_checker.distance_to_polyline_m(14.50, 75.81, pts) > 2500         # distance ignores the gap between the two rivers


def test_save_with_no_river_in_radius_is_a_404_with_advice(client, fake_world):
    fake_world["rivers"] = []
    before = len(table_select("rivers"))
    r = client.post("/api/live-location", json={"input": "14.5126, 75.805", "radius_km": 2})
    assert r.status_code == 404 and r.get_json()["code"] == "no_river_nearby" and "larger radius" in r.get_json()["error"]
    assert len(table_select("rivers")) == before


def _database_without(monkeypatch, missing):
    """A database that rejects the given columns one at a time - exactly how PostgREST reports a missing column."""
    real = routes.table_insert
    seen = []

    def insert(table, row):
        seen.append(sorted(row))
        bad = next((c for c in missing if c in row), None)
        if table == "rivers" and bad:
            raise Exception(f"Could not find the '{bad}' column of 'rivers' in the schema cache")
        return real(table, row)
    monkeypatch.setattr(routes, "table_insert", insert)
    return seen


def test_save_still_works_when_migration_0003_has_not_been_run(client, fake_world, monkeypatch):
    seen = _database_without(monkeypatch, routes.LIVE_EXTRA_COLUMNS)
    r = client.post("/api/live-location", json={"input": GOOGLE, "name": "Harihar point"})
    body = r.get_json()
    assert r.status_code == 201
    assert all(c not in seen[-1] for c in routes.LIVE_EXTRA_COLUMNS) and "monitor_points" in seen[-1]   # final save: no live columns, corridor kept
    assert "0003_live_areas.sql" in body["warnings"][0]
    assert body["river"]["name"] == "Harihar point" and body["river"]["place_name"] == PLACE["place_name"]   # still shown this session


def test_save_still_works_on_an_old_rivers_table_without_corridor_columns(client, fake_world, monkeypatch):
    seen = _database_without(monkeypatch, routes.LIVE_EXTRA_COLUMNS + ("monitor_points", "buffer_geojson"))
    r = client.post("/api/live-location", json={"input": GOOGLE, "name": "Old schema"})
    body = r.get_json()
    assert r.status_code == 201 and sorted(seen[-1]) == sorted(["name", "state", "district", "center_lat", "center_lng", "buffer_radius_m",
                                                                "scan_interval_days", "monitoring_enabled"])
    assert any("0001_init.sql" in w for w in body["warnings"]) and any("0003_live_areas.sql" in w for w in body["warnings"])
    assert len(body["river"]["monitor_points"]) == 8                                          # the response still carries the corridor


def test_a_genuine_database_error_is_not_swallowed(client, fake_world, monkeypatch):
    monkeypatch.setattr(routes, "table_insert", lambda t, r: (_ for _ in ()).throw(Exception("permission denied for table rivers")))
    r = client.post("/api/live-location", json={"input": GOOGLE})
    assert r.status_code == 500 and r.get_json()["code"] == "db_error" and "permission denied" in r.get_json()["error"]


def test_fix_corridor_keeps_a_live_area_instead_of_remapping_it_by_name(client, fake_world):
    river = client.post("/api/live-location", json={"input": GOOGLE, "name": "My sand point", "radius_km": 5}).get_json()["river"]
    fake_world["rivers"] = TWO_RIVERS[:1]                                         # the area now only finds one river
    monkey_calls = []
    import satellite.river_corridor as rc
    orig = rc.snap_to_river_center
    rc.snap_to_river_center = lambda *a, **k: monkey_calls.append(a) or (0, 0, [])
    try:
        ll.find_nearby_rivers = lambda lat, lng, r, **k: fake_world["rivers"]
        out = client.post(f"/api/rivers/{river['id']}/refresh-corridor").get_json()
    finally:
        rc.snap_to_river_center = orig
    assert monkey_calls == [] and len(out["monitor_points"]) == 4                  # re-found inside the radius, never snapped by name
    saved = next(r for r in client.get("/api/rivers").get_json() if r["id"] == river["id"])
    assert (saved["center_lat"], saved["center_lng"]) == (14.5126, 75.805) and saved["name"] == "My sand point"


def test_live_area_can_be_scanned_and_used_like_any_river(client, fake_world):
    river = client.post("/api/live-location", json={"input": GOOGLE, "name": "Area"}).get_json()["river"]
    import scheduler
    assert scheduler._due_for_scan({**river, "last_scanned_at": None}) is True   # monitored automatically like a river
