"""
Builds monitoring points strictly ON a river channel (never in nearby cities).

Uses OpenStreetMap Overpass to fetch waterway geometry, merges connected
river segments along flow direction, and samples points from upstream to
downstream along the riverbed only.
"""
import logging
import math
import re
import requests

logger = logging.getLogger(__name__)

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
HEADERS = {"User-Agent": "RiverGuard-SandMiningDetection/1.0 (final year project)"}


def _overpass_query(query):
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(url, data={"data": query}, headers=HEADERS, timeout=55)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"Overpass {url} failed: {e}")
    return None

# Only major channels — excludes random streams/canals in urban areas
RIVER_TYPES = {"river", "canal"}


def _haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _point_to_segment_dist_m(px, py, ax, ay, bx, by):
    lat_scale = 111320
    pxm, pym = px * lat_scale, py * lat_scale
    axm, aym = ax * lat_scale, ay * lat_scale
    bxm, bym = bx * lat_scale, by * lat_scale
    dx, dy = bxm - axm, bym - aym
    if dx == 0 and dy == 0:
        return math.hypot(pxm - axm, pym - aym)
    t = max(0, min(1, ((pxm - axm) * dx + (pym - aym) * dy) / (dx * dx + dy * dy)))
    return math.hypot(pxm - axm - t * dx, pym - aym - t * dy)


def _polyline_length(coords):
    total = 0.0
    for i in range(1, len(coords)):
        total += _haversine_m(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
    return total


def _endpoint_key(lat, lng, precision=4):
    return (round(lat, precision), round(lng, precision))


def _way_coords(way):
    return [(n["lat"], n["lon"]) for n in way.get("geometry", [])]


def _way_meta(way):
    tags = way.get("tags", {})
    return {
        "waterway": tags.get("waterway", ""),
        "name": (tags.get("name") or tags.get("name:en") or "").lower(),
    }


def _name_matches(way_name, query_name):
    if not way_name or not query_name:
        return False
    q = re.sub(r"\b(river|nadi|the)\b", "", query_name.lower()).strip()
    w = way_name.lower()
    return q in w or w in q or any(part in w for part in q.split() if len(part) > 3)


def _fetch_waterways(lat, lng, river_name=None, radius_m=30000):
    """Fetch river/canal ways from Overpass — prefer named river match."""
    queries = []
    if river_name:
        safe = re.sub(r'["\\]', "", river_name.strip())
        queries.append(f"""
        [out:json][timeout:45];
        (
          way["waterway"~"river|canal"]["name"~"{safe}",i](around:{radius_m},{lat},{lng});
        );
        out geom tags;
        """)
    queries.append(f"""
    [out:json][timeout:45];
    (
      way["waterway"="river"](around:{radius_m},{lat},{lng});
    );
    out geom tags;
    """)

    ways = []
    seen_ids = set()
    for q in queries:
        data = _overpass_query(q)
        if not data:
            continue
        for el in data.get("elements", []):
            if el.get("type") != "way" or not el.get("geometry"):
                continue
            wid = el.get("id")
            if wid in seen_ids:
                continue
            meta = _way_meta(el)
            if meta["waterway"] not in RIVER_TYPES:
                continue
            seen_ids.add(wid)
            ways.append(el)
        if ways and river_name and any(_name_matches(_way_meta(w)["name"], river_name) for w in ways):
            break
    return ways


def _closest_way_index(ways, lat, lng, river_name=None):
    best_i, best_score = None, float("-inf")
    for i, way in enumerate(ways):
        coords = _way_coords(way)
        if len(coords) < 2:
            continue
        dist = min(
            _point_to_segment_dist_m(lat, lng, coords[j][0], coords[j][1], coords[j + 1][0], coords[j + 1][1])
            for j in range(len(coords) - 1)
        )
        meta = _way_meta(way)
        length = _polyline_length(coords)
        score = -dist + length * 0.01
        if river_name and _name_matches(meta["name"], river_name):
            score += 5000
        if meta["waterway"] == "river":
            score += 500
        if score > best_score:
            best_score, best_i = score, i
    return best_i


def _merge_connected_chain(ways, seed_idx):
    """Extend seed way by connecting shared endpoints into one polyline."""
    if seed_idx is None:
        return []
    chains = [_way_coords(ways[seed_idx])]
    used = {seed_idx}

    def _try_extend(chain, forward=True):
        end = chain[-1] if forward else chain[0]
        end_key = _endpoint_key(end[0], end[1])
        best_j, best_extra, reverse = None, None, False
        for j, way in enumerate(ways):
            if j in used:
                continue
            c = _way_coords(way)
            if len(c) < 2:
                continue
            starts = (_endpoint_key(c[0][0], c[0][1]), _endpoint_key(c[-1][0], c[-1][1]))
            if starts[0] == end_key:
                best_j, best_extra, reverse = j, c, False
                break
            if starts[1] == end_key:
                best_j, best_extra, reverse = j, c, True
                break
        if best_j is None:
            return chain
        used.add(best_j)
        extra = list(reversed(best_extra)) if reverse else best_extra
        if forward:
            if _endpoint_key(chain[-1][0], chain[-1][1]) == _endpoint_key(extra[0][0], extra[0][1]):
                return chain + extra[1:]
            return chain + extra
        if _endpoint_key(chain[0][0], chain[0][1]) == _endpoint_key(extra[-1][0], extra[-1][1]):
            return extra[:-1] + chain
        return extra + chain

    for _ in range(40):
        before = len(chains[0])
        chains[0] = _try_extend(chains[0], forward=True)
        chains[0] = _try_extend(chains[0], forward=False)
        if len(chains[0]) == before:
            break
    return chains[0]


def _sample_along_polyline(coords, spacing_m=2000, max_points=12):
    if not coords or len(coords) < 2:
        return []

    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + _haversine_m(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1]))

    total = cum[-1]
    if total < 500:
        return [{"lat": round(coords[0][0], 6), "lng": round(coords[0][1], 6), "label": "river source", "on_river": True}]

    n = min(max_points, max(3, int(total / spacing_m) + 1))
    step = total / (n - 1) if n > 1 else total
    targets = [i * step for i in range(n)]

    points = []
    seg = 0
    labels = ["upstream (river start)", "mid-upstream", "river mid", "mid-downstream", "downstream (river end)"]
    for idx, target in enumerate(targets):
        while seg < len(cum) - 1 and cum[seg + 1] < target:
            seg += 1
        seg_len = cum[seg + 1] - cum[seg]
        frac = 0 if seg_len == 0 else (target - cum[seg]) / seg_len
        lat = coords[seg][0] + frac * (coords[seg + 1][0] - coords[seg][0])
        lng = coords[seg][1] + frac * (coords[seg + 1][1] - coords[seg][1])
        if idx == 0:
            label = labels[0]
        elif idx == len(targets) - 1:
            label = labels[-1]
        elif idx == len(targets) // 2:
            label = labels[2]
        else:
            label = f"river segment {idx + 1} (flow direction)"
        points.append({"lat": round(lat, 6), "lng": round(lng, 6), "label": label, "on_river": True})
    return points


def _validate_on_river(points, ways, max_dist_m=250):
    """Drop any point that drifted off the waterway (should not happen, safety check)."""
    if not ways:
        return points
    validated = []
    for pt in points:
        best = float("inf")
        for way in ways:
            coords = _way_coords(way)
            for j in range(len(coords) - 1):
                d = _point_to_segment_dist_m(
                    pt["lat"], pt["lng"],
                    coords[j][0], coords[j][1], coords[j + 1][0], coords[j + 1][1],
                )
                best = min(best, d)
        if best <= max_dist_m:
            validated.append(pt)
        else:
            logger.warning(f"Dropped off-river point ({pt['lat']}, {pt['lng']}) — {best:.0f}m from channel")
    return validated


def fetch_monitor_points(lat, lng, river_name=None, radius_m=30000, spacing_m=2000, max_points=12):
    """
    Returns [{lat, lng, label, on_river}, ...] sampled along the river channel
    from upstream to downstream. Never places points in cities or off-channel.
    """
    ways = _fetch_waterways(lat, lng, river_name=river_name, radius_m=radius_m)
    if not ways:
        logger.warning(f"No river waterway found near ({lat}, {lng}) for '{river_name}'")
        return []

    seed = _closest_way_index(ways, lat, lng, river_name=river_name)
    if seed is None:
        return []

    chain = _merge_connected_chain(ways, seed)
    if len(chain) < 2:
        return []

    points = _sample_along_polyline(chain, spacing_m=spacing_m, max_points=max_points)
    points = _validate_on_river(points, ways)
    if points:
        logger.info(
            f"Built {len(points)} on-river monitor points along { _polyline_length(chain)/1000:.1f}km "
            f"of '{river_name or 'waterway'}' channel."
        )
    return points


def snap_to_river_center(lat, lng, river_name=None, **kwargs):
    """
    Snaps to the midpoint of the on-river corridor (never a city center).
    Returns (center_lat, center_lng, monitor_points).
    """
    kwargs.setdefault("river_name", river_name)
    points = fetch_monitor_points(lat, lng, **kwargs)
    if not points:
        return lat, lng, []
    mid = points[len(points) // 2]
    return mid["lat"], mid["lng"], points
