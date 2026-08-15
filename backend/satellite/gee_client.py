"""
Satellite change detection via Google Earth Engine (Sentinel-2).

Real GEE integration requires a service account — see README for setup.
If GEE isn't configured, every function here falls back to clearly-labeled
synthetic data so the app is still demoable without credentials.
"""
import logging
import hashlib

import config

logger = logging.getLogger(__name__)

_gee_initialized = False


def _init_gee():
    global _gee_initialized
    if _gee_initialized:
        return True
    if not config.GEE_SERVICE_ACCOUNT or not config.GEE_PRIVATE_KEY_PATH:
        return False
    try:
        import ee
        credentials = ee.ServiceAccountCredentials(config.GEE_SERVICE_ACCOUNT, config.GEE_PRIVATE_KEY_PATH)
        project = config.GEE_PROJECT_ID
        if not project and "@" in config.GEE_SERVICE_ACCOUNT:
            project = config.GEE_SERVICE_ACCOUNT.split("@")[1].split(".")[0]
        if project:
            ee.Initialize(credentials, project=project)
        else:
            ee.Initialize(credentials)
        _gee_initialized = True
        logger.info("Google Earth Engine initialized successfully.")
        return True
    except Exception as e:
        logger.error(f"GEE init failed: {e}")
        return False


def is_configured():
    return _init_gee()


def _least_cloudy_image(point, date_str, cloud_max=40, search_window_days=60):
    import ee
    center = ee.Date(date_str)
    start = center.advance(-search_window_days // 2, "day")
    end = center.advance(search_window_days // 2, "day")
    collection = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                  .filterBounds(point)
                  .filterDate(start, end)
                  .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", cloud_max))
                  .sort("CLOUDY_PIXEL_PERCENTAGE"))
    if collection.size().getInfo() == 0 and cloud_max < 80:
        collection = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                      .filterBounds(point)
                      .filterDate(start, end)
                      .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
                      .sort("CLOUDY_PIXEL_PERCENTAGE"))
    if collection.size().getInfo() == 0:
        return None
    return collection.first()


def compute_ndwi_change(lat, lng, before_date, after_date, buffer_m=1500):
    """
    Real GEE path: pulls Sentinel-2 imagery for the point/date pair, computes
    NDWI (water index) for each, and returns the fractional change in
    water-adjacent sandbar area, plus true-color thumbnail URLs for both
    dates so the dashboard can show an actual visual before/after.
    Returns None if GEE isn't configured or no cloud-free image was found.
    """
    if not _init_gee():
        return None
    try:
        import ee
        point = ee.Geometry.Point([lng, lat])
        region = point.buffer(buffer_m).bounds()

        before_img = _least_cloudy_image(point, before_date)
        after_img = _least_cloudy_image(point, after_date)
        if before_img is None or after_img is None:
            raise ValueError("No cloud-free Sentinel-2 scene in search window")

        before_img.bandNames().getInfo()
        after_img.bandNames().getInfo()

        before_ndwi = before_img.normalizedDifference(["B3", "B8"]).rename("NDWI")
        after_ndwi = after_img.normalizedDifference(["B3", "B8"]).rename("NDWI")
        diff = after_ndwi.subtract(before_ndwi)

        # Negative NDWI change = water/wet-sand receded = exposed/removed sandbar
        stats = diff.reduceRegion(reducer=ee.Reducer.mean(), geometry=region, scale=10, maxPixels=1e9)
        change_val = stats.getInfo().get("NDWI", 0) or 0

        vis_params = {"bands": ["B4", "B3", "B2"], "min": 0, "max": 3000, "gamma": 1.2}
        before_thumb = before_img.clip(region).getThumbURL({**vis_params, "region": region, "dimensions": 512})
        after_thumb = after_img.clip(region).getThumbURL({**vis_params, "region": region, "dimensions": 512})

        return {
            "change_score": min(1.0, abs(change_val) * 3),
            "raw_ndwi_diff": round(float(change_val), 4),
            "before_thumb_url": before_thumb,
            "after_thumb_url": after_thumb,
            "synthetic": False,
        }
    except Exception as e:
        logger.warning(f"NDWI computation failed (likely no cloud-free scene in window): {e}")
        return None


def get_before_after_pair(lat, lng, before_date, after_date):
    """
    Returns satellite change info for a location. Tries real GEE first,
    falls back to a deterministic synthetic value (seeded on coordinates
    so results are stable/repeatable for the same location) for demo mode.
    """
    result = compute_ndwi_change(lat, lng, before_date, after_date)
    if result is not None:
        return result

    seed = int(hashlib.sha256(f"{lat:.3f},{lng:.3f},{before_date},{after_date}".encode()).hexdigest(), 16) % 1000
    change_score = round((seed % 100) / 100.0, 2)
    logger.info("GEE not configured or no clear scene found — returning synthetic satellite change score for demo purposes.")
    return {
        "change_score": change_score,
        "raw_ndwi_diff": None,
        "before_thumb_url": None,
        "after_thumb_url": None,
        "synthetic": True,
    }


def download_thumbnail(url, dest_path):
    """Downloads a GEE thumbnail URL to a local file. Returns True on success."""
    import requests
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        logger.error(f"Thumbnail download failed: {e}")
        return False
