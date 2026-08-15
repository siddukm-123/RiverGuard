"""
Zone checker — adapted from the original sand-mining-detection zip's
zone_builder.py. Same idea (buffer polygon around a river centerline,
point-in-polygon test) but reworked to check either:
  (a) a GPS point (from a satellite detection), or
  (b) nothing at all (a manual video/image upload with no GPS) — in which
      case it's reported as "zone unknown" rather than guessed at.
"""
import logging

logger = logging.getLogger(__name__)


def build_buffer_polygon(centerline_coords, radius_m=1000.0):
    """
    Builds a buffer polygon (in WGS84 lon/lat) around a river centerline.
    centerline_coords: list of [lon, lat] pairs.
    Returns a GeoJSON-like dict, or None if geopandas/shapely aren't available
    or the centerline is empty.
    """
    if not centerline_coords:
        return None
    try:
        import geopandas as gpd
        from shapely.geometry import LineString
        import warnings
        warnings.filterwarnings("ignore", category=UserWarning)

        line = LineString(centerline_coords)
        gdf = gpd.GeoDataFrame(geometry=[line], crs="EPSG:4326")
        # Project to a metric CRS to buffer in real metres, then back to WGS84
        gdf_m = gdf.to_crs("EPSG:32644")  # UTM zone covering most of peninsular India; adjust per region if needed
        buf = gdf_m.geometry.buffer(radius_m, cap_style=2, join_style=1, resolution=16)
        buf_gdf = gpd.GeoDataFrame(geometry=buf, crs="EPSG:32644").to_crs("EPSG:4326")
        return buf_gdf.iloc[0].geometry.__geo_interface__
    except Exception as e:
        logger.error(f"Buffer build failed: {e}")
        return None


def point_in_buffer(lat, lng, buffer_geojson):
    """Returns True if (lat, lng) falls inside the given buffer GeoJSON polygon."""
    if not buffer_geojson:
        return None  # unknown, not False — important distinction for the reasoning engine
    try:
        from shapely.geometry import Point, shape
        point = Point(lng, lat)  # GeoJSON is (lon, lat)
        polygon = shape(buffer_geojson)
        return polygon.contains(point)
    except Exception as e:
        logger.error(f"Point-in-buffer check failed: {e}")
        return None


def simple_distance_check(lat, lng, river_lat, river_lng, radius_m=1000.0):
    """
    Fallback zone check when no real centerline/buffer polygon exists yet
    (e.g. a demo river that's only got a center point). Uses a flat-earth
    approximation which is fine at the ~1km scale this matters at.
    """
    import math
    if lat is None or lng is None:
        return None
    dy = (lat - river_lat) * 111320
    dx = (lng - river_lng) * 111320 * math.cos(math.radians(river_lat))
    dist_m = math.sqrt(dx ** 2 + dy ** 2)
    return dist_m <= radius_m
