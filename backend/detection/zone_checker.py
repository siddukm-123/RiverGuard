"""
Zone checker - is a GPS point inside the protected buffer around a river?

Three methods, best first:
  1. buffer polygon (GeoJSON) built from the river's corridor centre-line - proper point-in-polygon
  2. distance to the corridor poly-line (no polygon stored yet) - metres to the nearest segment
  3. distance to the river's centre point (legacy last resort)
Returns True / False, or None when the location is unknown (None is NOT False).

The buffer is built in the correct UTM zone for the river's longitude. The first version
hard-coded EPSG:32644 (zone 44N), which is wrong for most of India (Karnataka lies in 43N,
Assam in 46N) and distorts the buffer distance by up to a few percent.
"""
import logging
import math

logger = logging.getLogger(__name__)


def utm_epsg_for(lat, lng):
    """EPSG code of the UTM zone containing (lat, lng)."""
    zone = int((lng + 180.0) // 6.0) + 1
    zone = max(1, min(60, zone))
    return (32600 if lat >= 0 else 32700) + zone


def build_buffer_polygon(centerline_coords, radius_m=1000.0):
    """
    centerline_coords: [[lon, lat], ...]. Returns a GeoJSON geometry dict (WGS84) or None.
    Buffers in metres in the local UTM zone, then converts back to lon/lat.
    """
    if not centerline_coords or len(centerline_coords) < 2:
        return None
    try:
        from pyproj import Transformer
        from shapely.geometry import LineString, mapping
        from shapely.ops import transform

        mid_lon = sum(c[0] for c in centerline_coords) / len(centerline_coords)
        mid_lat = sum(c[1] for c in centerline_coords) / len(centerline_coords)
        epsg = utm_epsg_for(mid_lat, mid_lon)
        to_m = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform
        to_deg = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True).transform
        line_m = transform(to_m, LineString(centerline_coords))
        buf_m = line_m.buffer(float(radius_m), cap_style=1, join_style=1, resolution=16)
        poly = transform(to_deg, buf_m).simplify(0.00002)
        geo = mapping(poly)
        geo["properties_note"] = f"buffer {radius_m:g} m around corridor, UTM EPSG:{epsg}"
        return geo
    except Exception as e:
        logger.error(f"Buffer build failed: {e}")
        return None


def buffer_from_monitor_points(monitor_points, radius_m=1000.0):
    pts = [[p["lng"], p["lat"]] for p in (monitor_points or []) if p.get("lat") is not None and p.get("lng") is not None]
    return build_buffer_polygon(pts, radius_m)


def point_in_buffer(lat, lng, buffer_geojson):
    """True/False, or None if there is no polygon or the geometry is unusable."""
    if not buffer_geojson or lat is None or lng is None:
        return None
    try:
        from shapely.geometry import Point, shape
        geo = {k: v for k, v in buffer_geojson.items() if k != "properties_note"}
        return bool(shape(geo).contains(Point(lng, lat)))
    except Exception as e:
        logger.error(f"Point-in-buffer check failed: {e}")
        return None


def _seg_dist_m(lat, lng, a, b):
    """Distance (m) from a point to segment a-b using a local equirectangular projection."""
    k_lat = 111320.0
    k_lng = 111320.0 * math.cos(math.radians(lat))
    px, py = 0.0, 0.0
    ax, ay = (a["lng"] - lng) * k_lng, (a["lat"] - lat) * k_lat
    bx, by = (b["lng"] - lng) * k_lng, (b["lat"] - lat) * k_lat
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(ax - px, ay - py)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def distance_to_polyline_m(lat, lng, monitor_points):
    pts = [p for p in (monitor_points or []) if p.get("lat") is not None]
    if not pts:
        return None
    if len(pts) == 1:
        return _seg_dist_m(lat, lng, pts[0], pts[0])
    return min(_seg_dist_m(lat, lng, pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def polyline_distance_check(lat, lng, monitor_points, radius_m=1000.0):
    if lat is None or lng is None:
        return None
    d = distance_to_polyline_m(lat, lng, monitor_points)
    return None if d is None else d <= radius_m


def simple_distance_check(lat, lng, river_lat, river_lng, radius_m=1000.0):
    """Legacy: distance to the river's centre point only (flat-earth, fine at ~1 km)."""
    if lat is None or lng is None:
        return None
    dy = (lat - river_lat) * 111320
    dx = (lng - river_lng) * 111320 * math.cos(math.radians(river_lat))
    return math.sqrt(dx ** 2 + dy ** 2) <= radius_m
