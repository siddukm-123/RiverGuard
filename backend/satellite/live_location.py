"""
Live location: the user pastes a Google Maps link (or plain coordinates) and RiverGuard
  1. extracts the exact coordinates,
  2. looks up the real place name (reverse geocoding, OpenStreetMap Nominatim),
  3. finds every river that passes within the chosen radius of that spot (OpenStreetMap Overpass) and builds
     monitoring points ON those river channels, clipped to the radius.

Nothing is invented: if no river is found in the radius, the caller is told so.
"""
import logging
import math
import re
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests

from satellite import river_corridor as rc

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "RiverGuard-SandMiningDetection/1.0 (final year project)"}
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
MIN_RADIUS_KM, MAX_RADIUS_KM, DEFAULT_RADIUS_KM = 1.0, 15.0, 5.0


class LocationError(ValueError):
    """Bad / unusable user input. The message is safe to show to the user."""


# ── 1. coordinates from a pasted link / text ─────────────────────────────
_NUM = r"-?\d{1,3}\.\d+"          # decimals required: "12, 34" or "Tower 12, 34th Street" is never read as a location
_PAIR = re.compile(rf"(?<![\d.])({_NUM})\s*[,;\s]\s*({_NUM})(?![\d.])")
_PIN = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")          # exact pin of a Google "place"
_AT = re.compile(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")                # map centre
_DMS = re.compile(r"""(\d{1,3})\s*[°º]\s*(\d{1,2})\s*['′’]\s*(\d{1,2}(?:\.\d+)?)\s*["″”]?\s*([NSns])[\s,+]*"""
                  r"""(\d{1,3})\s*[°º]\s*(\d{1,2})\s*['′’]\s*(\d{1,2}(?:\.\d+)?)\s*["″”]?\s*([EWew])""")
_HEMI = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*°?\s*([NSns])[\s,;]+(-?\d{1,3}(?:\.\d+)?)\s*°?\s*([EWew])")
_QUERY_KEYS = ("q", "ll", "query", "center", "destination", "daddr", "saddr")
SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl"}


def _valid(lat, lng):
    return -90 <= lat <= 90 and -180 <= lng <= 180 and not (lat == 0 and lng == 0)


def _from_pair(a, b):
    lat, lng = float(a), float(b)
    return (lat, lng) if _valid(lat, lng) else None


def _dms(deg, mnt, sec, hemi):
    v = float(deg) + float(mnt) / 60 + float(sec) / 3600
    return -v if hemi.upper() in "SW" else v


def coords_from_text(text):
    """(lat, lng) from plain text or a (long) Google Maps URL, or None. No network."""
    t = unquote(unquote(text or "")).replace("+", " ")
    m = _DMS.search(t)
    if m:
        lat = _dms(m.group(1), m.group(2), m.group(3), m.group(4))
        lng = _dms(m.group(5), m.group(6), m.group(7), m.group(8))
        if _valid(lat, lng):
            return lat, lng
    m = _PIN.search(t)
    if m and _from_pair(m.group(1), m.group(2)):
        return _from_pair(m.group(1), m.group(2))
    m = _AT.search(t)
    if m and _from_pair(m.group(1), m.group(2)):
        return _from_pair(m.group(1), m.group(2))
    if t.lower().startswith("http"):
        parsed = urlparse(t)
        qs = parse_qs(parsed.query)
        for key in _QUERY_KEYS:
            for val in qs.get(key, []):
                got = coords_from_text(val) if not val.lower().startswith("http") else None
                if got:
                    return got
        t = parsed.path + " " + parsed.query                                     # last resort: a coordinate pair in the path
    m = _HEMI.search(t)
    if m:
        lat = float(m.group(1)) * (-1 if m.group(2).upper() == "S" else 1)
        lng = float(m.group(3)) * (-1 if m.group(4).upper() == "W" else 1)
        if _valid(lat, lng):
            return lat, lng
    m = _PAIR.search(t)
    if m:
        return _from_pair(m.group(1), m.group(2))
    return None


# ── short links (maps.app.goo.gl/...) - resolved safely ──────────────────
def _is_google_host(host):
    host = (host or "").lower().split(":")[0]
    return host in SHORT_HOSTS or bool(re.fullmatch(r"(?:[a-z0-9-]+\.)*google\.(?:com|co\.[a-z]{2}|[a-z]{2,3})", host))


def resolve_short_link(url, max_hops=6, getter=None):
    """
    Follows the redirects of a Google short link and returns the final (long) URL. Only Google hosts are ever contacted
    (initial URL AND every redirect target), so a pasted link can never make the server call an arbitrary address.
    """
    getter = getter or (lambda u: requests.get(u, allow_redirects=False, timeout=8, headers=HEADERS))
    current = url
    for _ in range(max_hops):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https") or not _is_google_host(parsed.hostname):
            raise LocationError("That link is not a Google Maps link. Paste a link from Google Maps (Share → Copy link) or type the coordinates.")
        if parsed.hostname and parsed.hostname.startswith("consent."):          # EU cookie wall: the real URL is in ?continue=
            cont = parse_qs(parsed.query).get("continue", [""])[0]
            if cont:
                current = cont
                continue
        if coords_from_text(current) and parsed.hostname not in SHORT_HOSTS:
            return current                                                      # already a long link with coordinates
        try:
            resp = getter(current)
        except requests.RequestException as e:
            raise LocationError("Could not open that Google Maps short link right now. Open it in Google Maps and copy the full link "
                                f"or the coordinates instead. ({e.__class__.__name__})")
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            current = requests.compat.urljoin(current, resp.headers["Location"])
            continue
        return current
    raise LocationError("That Google Maps link redirects too many times. Open it in Google Maps and copy the full link or the coordinates.")


def parse_location(text, getter=None):
    """
    Returns {"lat", "lng", "source"} where source is "coordinates", "google_maps" or "google_short_link".
    Raises LocationError with a friendly message when nothing usable is found.
    """
    raw = (text or "").strip()
    if not raw:
        raise LocationError("Paste a Google Maps link or coordinates (for example 14.4644, 75.9218).")
    if len(raw) > 2000:
        raise LocationError("That text is too long to be a location.")
    if re.match(r"^[a-z][a-z0-9+.\-]*://", raw, re.I) and not raw.lower().startswith(("http://", "https://")):
        raise LocationError("That link is not a Google Maps link. Paste a link from Google Maps (Share → Copy link) or type the coordinates.")
    first = raw.split()[0] if raw.lower().startswith("http") else raw
    source = "coordinates"
    if first.lower().startswith("http"):
        host = (urlparse(first).hostname or "").lower()
        if not _is_google_host(host):
            raise LocationError("That link is not a Google Maps link. Paste a link from Google Maps (Share → Copy link) or type the coordinates.")
        source = "google_maps"
        if host in SHORT_HOSTS:
            source = "google_short_link"
            first = resolve_short_link(first, getter=getter)
        raw = first
    got = coords_from_text(raw)
    if not got:
        raise LocationError("Could not find coordinates in that. In Google Maps, right-click the spot and click the coordinates to copy them, "
                            "or use Share → Copy link, then paste here.")
    return {"lat": round(got[0], 6), "lng": round(got[1], 6), "source": source}


# ── 2. the real place name ───────────────────────────────────────────────
_REV_CACHE, _REV_LOCK = {}, threading.Lock()


def reverse_geocode(lat, lng, http_get=None):
    """{"place_name", "locality", "district", "state", "country", "display_name"} or None if the lookup is unavailable."""
    key = (round(lat, 3), round(lng, 3))
    with _REV_LOCK:
        if key in _REV_CACHE:
            return _REV_CACHE[key]
    try:
        get = http_get or requests.get
        resp = get(NOMINATIM_REVERSE, params={"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 14, "addressdetails": 1,
                                              "accept-language": "en"}, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Reverse geocoding failed for ({lat}, {lng}): {e}")
        return None
    addr = data.get("address") or {}
    locality = next((addr[k] for k in ("village", "town", "city", "suburb", "hamlet", "neighbourhood", "locality") if addr.get(k)), "")
    district = addr.get("county") or addr.get("state_district") or addr.get("city_district") or ""
    if district and not re.search(r"district|taluk|tehsil", district, re.I) and addr.get("country_code") == "in":
        district += " district"
    parts = []
    for p in (locality, district, addr.get("state"), addr.get("country")):
        if p and p not in parts:
            parts.append(p)
    if not parts:
        return None
    out = {"place_name": ", ".join(parts), "locality": locality, "district": district.replace(" district", ""),
           "state": addr.get("state") or "", "country": addr.get("country") or "", "display_name": data.get("display_name") or ""}
    with _REV_LOCK:
        _REV_CACHE[key] = out
    return out


# ── 3. rivers inside the radius ──────────────────────────────────────────
def _local_xy(lat0, lng0, lat, lng):
    k = 111320.0
    return (lng - lng0) * math.cos(math.radians(lat0)) * k, (lat - lat0) * k


def _dist_point_to_polyline_m(lat, lng, coords):
    best = float("inf")
    px, py = 0.0, 0.0
    for i in range(len(coords) - 1):
        ax, ay = _local_xy(lat, lng, *coords[i])
        bx, by = _local_xy(lat, lng, *coords[i + 1])
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        best = min(best, math.hypot(ax + t * dx - px, ay + t * dy - py))
    return best


def _densify(coords, step_m=200):
    out = [coords[0]]
    for a, b in zip(coords, coords[1:]):
        d = rc._haversine_m(a[0], a[1], b[0], b[1])
        n = max(1, int(d // step_m))
        for k in range(1, n + 1):
            f = k / n
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return out


def _longest_run_inside(coords, lat, lng, radius_m):
    runs, cur = [], []
    for c in coords:
        if rc._haversine_m(lat, lng, c[0], c[1]) <= radius_m:
            cur.append(c)
        else:
            if len(cur) > 1:
                runs.append(cur)
            cur = []
    if len(cur) > 1:
        runs.append(cur)
    return max(runs, key=rc._polyline_length) if runs else []


def fetch_waterways_around(lat, lng, radius_m, timeout=25):
    """Raw Overpass ways (with geometry) of rivers/canals within radius; falls back to NAMED streams (small seasonal rivers)."""
    around = f"(around:{int(radius_m)},{lat},{lng})"
    queries = [f'[out:json][timeout:{timeout}];way["waterway"~"^(river|canal)$"]{around};out geom tags;',
               f'[out:json][timeout:{timeout}];way["waterway"="stream"]["name"]{around};out geom tags;']
    for q in queries:
        data = None
        for url in rc.OVERPASS_URLS:
            try:
                resp = requests.post(url, data={"data": q}, headers=HEADERS, timeout=timeout + 5)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Overpass {url} failed: {e}")
        if data is None:
            raise RuntimeError("OpenStreetMap river data is not reachable right now")
        ways = [e for e in data.get("elements", []) if e.get("type") == "way" and e.get("geometry")]
        if ways:
            return ways
    return []


def _way_name(way):
    tags = way.get("tags", {})
    return (tags.get("name:en") or tags.get("name") or "").strip()


def find_nearby_rivers(lat, lng, radius_km=DEFAULT_RADIUS_KM, max_rivers=3, total_points=12, fetch=None):
    """
    Rivers within radius_km of (lat, lng), nearest first:
      [{"name", "distance_km", "length_km", "points": [{lat, lng, label, on_river, river}]}]
    Points are sampled ON the channel and only from the part of the river that lies inside the radius.
    """
    radius_m = radius_km * 1000.0
    ways = (fetch or fetch_waterways_around)(lat, lng, radius_m)
    groups = {}
    for w in ways:
        name = _way_name(w)
        groups.setdefault(name.lower() or f"~unnamed-{w.get('id')}", []).append(w)

    rivers = []
    for key, gways in groups.items():
        seed = rc._closest_way_index(gways, lat, lng, river_name=None)
        chain = rc._merge_connected_chain(gways, seed if seed is not None else 0)
        if len(chain) < 2:
            continue
        dist = _dist_point_to_polyline_m(lat, lng, chain)
        run = _longest_run_inside(_densify(chain), lat, lng, radius_m)
        length = rc._polyline_length(run) if run else 0
        if dist > radius_m or length < 500:
            continue
        name = _way_name(gways[0]) or "Unnamed river"
        rivers.append({"name": name, "_dist": dist, "_len": length, "_run": run, "_named": not key.startswith("~unnamed")})

    named = [r for r in rivers if r["_named"]]
    rivers = sorted(named or rivers, key=lambda r: (r["_dist"], -r["_len"]))[:max_rivers]
    if not rivers:
        return []

    total_len = sum(r["_len"] for r in rivers)
    counts = [max(2, round(total_points * r["_len"] / total_len)) for r in rivers]
    while sum(counts) > total_points and max(counts) > 2:
        counts[counts.index(max(counts))] -= 1

    out = []
    for r, n in zip(rivers, counts):
        spacing = max(500.0, r["_len"] / max(1, n - 1))
        pts = rc._sample_along_polyline(r["_run"], spacing_m=spacing, max_points=n)
        for i, p in enumerate(pts, 1):
            p["label"] = f"{r['name']} · point {i}/{len(pts)}"
            p["river"] = r["name"]
        out.append({"name": r["name"], "distance_km": round(r["_dist"] / 1000, 1), "length_km": round(r["_len"] / 1000, 1), "points": pts})
    return out


_AREA_CACHE, _AREA_LOCK = {}, threading.Lock()
_AREA_TTL_S = 600


def find_nearby_rivers_cached(lat, lng, radius_km, **kw):
    """Preview and save ask for the same data seconds apart; the slow Overpass call is made only once."""
    key = (round(lat, 4), round(lng, 4), float(radius_km))
    now = time.time()
    with _AREA_LOCK:
        hit = _AREA_CACHE.get(key)
        if hit and now - hit[0] < _AREA_TTL_S:
            return hit[1]
    rivers = find_nearby_rivers(lat, lng, radius_km, **kw)
    with _AREA_LOCK:
        _AREA_CACHE[key] = (now, rivers)
        for k in [k for k, v in _AREA_CACHE.items() if now - v[0] > _AREA_TTL_S]:
            _AREA_CACHE.pop(k, None)
    return rivers


def clamp_radius_km(value):
    try:
        r = float(value)
    except (TypeError, ValueError):
        raise LocationError(f"Radius must be a number between {MIN_RADIUS_KM:g} and {MAX_RADIUS_KM:g} km.")
    if not (MIN_RADIUS_KM <= r <= MAX_RADIUS_KM):
        raise LocationError(f"Radius must be between {MIN_RADIUS_KM:g} and {MAX_RADIUS_KM:g} km.")
    return r
