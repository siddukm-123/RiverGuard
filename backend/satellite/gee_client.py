"""
Satellite change detection with Google Earth Engine (Sentinel-2 SR Harmonized).

What was wrong before (and is fixed here)
  1. Scene pairing: each date was expanded to +-30 days and the least-cloudy scene returned, so for
     dates 5 days apart both windows overlapped and the SAME scene was often used as 'before' and
     'after' (paper, Table II). Now: 'after' = most recent clean scene in a short window; 'before' =
     clean scene closest to (after - SAT_BASELINE_DAYS); the two must be different acquisitions at
     least SAT_MIN_SEPARATION_DAYS apart, otherwise NO comparison is produced.
  2. Cloud filtering used the whole-tile CLOUDY_PIXEL_PERCENTAGE. Now the SCL band is used to mask
     cloud/shadow/cirrus per pixel and the clean fraction INSIDE the area of interest is required.
  3. The metric was abs(mean dNDWI) over a 1.5 km buffer that is mostly land/open water - dilution
     and sign loss. Now it is measured inside a river-bed mask (JRC Global Surface Water occurrence
     > 5 %, dilated 30 m): water area, non-water/non-vegetated ("candidate exposed bed/sand") area,
     newly exposed / newly covered area, and the fraction of bed area with |dNDWI| > 0.3.
  4. When GEE was configured but no scene was found the old code fabricated a random 'synthetic'
     score and raised an alert (and notified officers). Now a configured-but-failing scan returns a
     status ('no_scene', 'insufficient_separation', 'error') and NO numbers. Synthetic demo data is
     produced ONLY when GEE credentials are absent, is flagged `synthetic: True`, and is never
     notified.

NDWI = (B3 - B8) / (B3 + B8)  (McFeeters 1996, green vs NIR).  Water pixel: NDWI > 0.
Candidate exposed bed: NDWI <= 0 and NDVI (B8-B4)/(B8+B4) < 0.3. This is a spectral proxy, not a
validated sand classifier, and bare ground changes for many natural reasons.

NOTE: this module needs live GEE credentials to run; it could not be exercised against the real
service in the build sandbox. The pure-python selection logic is unit-tested.
"""
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
_gee_initialized = False


# ── init ────────────────────────────────────────────────────────────────
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
        ee.Initialize(credentials, project=project) if project else ee.Initialize(credentials)
        _gee_initialized = True
        logger.info("Google Earth Engine initialized successfully.")
        return True
    except Exception as e:
        logger.error(f"GEE init failed: {e}")
        return False


def is_configured():
    return _init_gee()


def credentials_present():
    return bool(config.GEE_SERVICE_ACCOUNT and config.GEE_PRIVATE_KEY_PATH)


# ── pure scene-selection logic (unit-tested) ────────────────────────────
def _d(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)


def choose_after(cands, min_valid):
    """Most recent scene whose clean fraction inside the AOI is >= min_valid."""
    ok = [c for c in cands if c["valid"] >= min_valid]
    return max(ok, key=lambda c: (c["time_ms"], c["valid"])) if ok else None


def choose_before(cands, target_ms, min_valid):
    """Clean scene closest in time to target_ms (ties -> cleaner)."""
    ok = [c for c in cands if c["valid"] >= min_valid]
    return min(ok, key=lambda c: (abs(c["time_ms"] - target_ms), -c["valid"])) if ok else None


def check_pair(before, after, min_sep_days):
    """Returns (ok, reason). Rejects identical or too-close acquisitions."""
    if before is None or after is None:
        return False, "missing scene"
    if before["id"] == after["id"]:
        return False, "before and after are the same scene"
    sep = (after["time_ms"] - before["time_ms"]) / 86400000.0
    if sep < min_sep_days:
        return False, f"scenes are only {sep:.1f} days apart (minimum {min_sep_days})"
    return True, f"{sep:.0f} days apart"


def _fmt(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


# ── Earth Engine calls ──────────────────────────────────────────────────
def _valid_mask(img):
    scl = img.select("SCL")
    return scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6)).Or(scl.eq(7))  # vegetation, bare, water, unclassified


def _aoi(lat, lng, buffer_m):
    import ee
    point = ee.Geometry.Point([lng, lat])
    return point, point.buffer(buffer_m).bounds()


def list_candidates(lat, lng, start_date, end_date, buffer_m=1500):
    """All S2 scenes touching the point in [start, end) with their clean-pixel fraction INSIDE the AOI."""
    import ee
    point, aoi = _aoi(lat, lng, buffer_m)
    col = ee.ImageCollection(S2_COLLECTION).filterBounds(point).filterDate(start_date, end_date)

    def add_valid(img):
        frac = _valid_mask(img).rename("valid").reduceRegion(
            reducer=ee.Reducer.mean(), geometry=aoi, scale=20, maxPixels=1e8).get("valid")
        return img.set("aoi_valid", ee.Algorithms.If(frac, frac, 0))

    col = col.map(add_valid)
    data = ee.Dictionary({
        "ids": col.aggregate_array("system:index"),
        "times": col.aggregate_array("system:time_start"),
        "valid": col.aggregate_array("aoi_valid"),
    }).getInfo()
    out = []
    for i, t, v in zip(data.get("ids", []), data.get("times", []), data.get("valid", [])):
        out.append({"id": i, "time_ms": int(t), "date": _fmt(int(t)), "valid": float(v or 0)})
    return out


def compute_pair_metrics(lat, lng, before_id, after_id, buffer_m=1500):
    """River-bed-masked change metrics for one chosen scene pair. One server round-trip."""
    import ee
    b = ee.Image(f"{S2_COLLECTION}/{before_id}")
    a = ee.Image(f"{S2_COLLECTION}/{after_id}")
    _, aoi = _aoi(lat, lng, buffer_m)

    both = _valid_mask(b).And(_valid_mask(a))
    ndwi_b, ndwi_a = b.normalizedDifference(["B3", "B8"]), a.normalizedDifference(["B3", "B8"])
    ndvi_b, ndvi_a = b.normalizedDifference(["B8", "B4"]), a.normalizedDifference(["B8", "B4"])
    diff = ndwi_a.subtract(ndwi_b)

    gsw = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence").unmask(0)
    river = gsw.gt(5).focal_max(radius=30, kernelType="circle", units="meters")
    use = both.And(river)

    water_b, water_a = ndwi_b.gt(0), ndwi_a.gt(0)
    bare_b = water_b.Not().And(ndvi_b.lt(0.3))
    bare_a = water_a.Not().And(ndvi_a.lt(0.3))
    changed = diff.abs().gt(0.3)

    bands = ee.Image.cat([
        use.rename("area"),
        water_b.And(use).rename("water_before"),
        water_a.And(use).rename("water_after"),
        bare_b.And(use).rename("bare_before"),
        bare_a.And(use).rename("bare_after"),
        bare_a.And(bare_b.Not()).And(use).rename("bare_gain"),
        bare_b.And(bare_a.Not()).And(use).rename("bare_loss"),
        changed.And(use).rename("changed"),
        both.rename("area_aoi"),
        changed.And(both).rename("changed_aoi"),
    ]).multiply(ee.Image.pixelArea())

    sums = bands.reduceRegion(reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9, tileScale=4)
    mean_mask = diff.updateMask(use).reduceRegion(reducer=ee.Reducer.mean(), geometry=aoi, scale=10, maxPixels=1e9, tileScale=4)
    mean_aoi = diff.updateMask(both).reduceRegion(reducer=ee.Reducer.mean(), geometry=aoi, scale=10, maxPixels=1e9, tileScale=4)
    res = ee.Dictionary({"s": sums, "m_mask": mean_mask, "m_aoi": mean_aoi}).getInfo()

    s = {k: float(v or 0) for k, v in res["s"].items()}
    use_mask = s.get("area", 0) >= 50000  # >= 0.05 km2 of river bed inside the AOI (500 px at 10 m)
    def _first(d):
        return next(iter(d.values()), None) if d else None

    if use_mask:
        area, changed_m2, mean_d = s["area"], s["changed"], _first(res["m_mask"])
        mask_source = "jrc_gsw_occurrence>5%_dilated_30m"
    else:
        area, changed_m2, mean_d = s.get("area_aoi", 0), s.get("changed_aoi", 0), _first(res["m_aoi"])
        mask_source = "aoi_fallback_no_river_mask"
    if area <= 0:
        return None
    return {
        "mask_source": mask_source,
        "analysed_area_m2": round(area),
        "water_before_m2": round(s["water_before"]) if use_mask else None,
        "water_after_m2": round(s["water_after"]) if use_mask else None,
        "bare_before_m2": round(s["bare_before"]) if use_mask else None,
        "bare_after_m2": round(s["bare_after"]) if use_mask else None,
        "bare_gain_m2": round(s["bare_gain"]) if use_mask else None,
        "bare_loss_m2": round(s["bare_loss"]) if use_mask else None,
        "changed_fraction": round(changed_m2 / area, 4),
        "mean_ndwi_diff": None if mean_d is None else round(float(mean_d), 4),
    }


def thumbnails(lat, lng, before_id, after_id, buffer_m=1500):
    """True-colour thumbnail URLs (B4,B3,B2) for the two chosen scenes."""
    import ee
    _, region = _aoi(lat, lng, buffer_m)
    vis = {"bands": ["B4", "B3", "B2"], "min": 0, "max": 3000, "gamma": 1.2}
    out = []
    for sid in (before_id, after_id):
        img = ee.Image(f"{S2_COLLECTION}/{sid}").clip(region)
        out.append(img.getThumbURL({**vis, "region": region, "dimensions": 512}))
    return out[0], out[1]


def rainfall_between(lat, lng, date_a, date_b):
    """Mean CHIRPS rainfall (mm) over ~5 km around the point between two dates; None if unavailable."""
    try:
        import ee
        region = ee.Geometry.Point([lng, lat]).buffer(5000)
        total = (ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterDate(date_a, date_b).select("precipitation").sum()
                 .reduceRegion(reducer=ee.Reducer.mean(), geometry=region, scale=5566, maxPixels=1e8).getInfo())
        v = (total or {}).get("precipitation")
        return None if v is None else round(float(v), 1)
    except Exception as e:
        logger.warning(f"Rainfall context unavailable: {e}")
        return None


# ── public entry point ──────────────────────────────────────────────────
def _synthetic(lat, lng, before_date, after_date):
    seed = int(hashlib.sha256(f"{lat:.3f},{lng:.3f},{before_date},{after_date}".encode()).hexdigest(), 16) % 1000
    score = round((seed % 100) / 100.0, 2)
    return {
        "status": "ok", "synthetic": True, "method": "synthetic_demo", "change_score": score,
        "changed_fraction": round(score * 0.25, 4), "mean_ndwi_diff": None, "mask_source": None,
        "before_scene": None, "after_scene": None, "message": "SYNTHETIC demo value - Google Earth Engine is not configured.",
    }


def get_before_after_pair(lat, lng, before_date, after_date):
    """
    Returns a dict with `status`:
      ok                      real comparison; metrics + both scenes (id/date/clean fraction)
      no_scene                no sufficiently clean scene in a search window
      insufficient_separation scenes found but identical / too close in time
      error                   Earth Engine call failed
    When GEE credentials are absent returns an explicitly synthetic demo value instead.
    `before_date` may be None -> baseline = after scene date - SAT_BASELINE_DAYS.
    """
    if not credentials_present():
        return _synthetic(lat, lng, before_date, after_date)
    if not _init_gee():
        return {"status": "error", "synthetic": False, "message": "Google Earth Engine credentials are set but initialisation failed (check logs / IAM roles)."}

    half = config.SAT_SEARCH_HALF_WINDOW_DAYS
    try:
        after_target = _d(after_date)
        a_start = (after_target - timedelta(days=2 * half)).strftime("%Y-%m-%d")
        a_end = (after_target + timedelta(days=1)).strftime("%Y-%m-%d")
        after_cands = list_candidates(lat, lng, a_start, a_end)
        after = choose_after(after_cands, config.SAT_MIN_VALID_FRACTION)
        if after is None:
            return {"status": "no_scene", "synthetic": False,
                    "message": f"No Sentinel-2 scene with >= {config.SAT_MIN_VALID_FRACTION:.0%} clean pixels between {a_start} and {a_end} "
                               f"({len(after_cands)} scene(s) found, all too cloudy/shadowed or none)."}

        if before_date:
            before_target_ms = int(_d(before_date).timestamp() * 1000)
        else:
            before_target_ms = after["time_ms"] - config.SAT_BASELINE_DAYS * 86400000
        b_start = _fmt(before_target_ms - half * 86400000)
        b_end = _fmt(before_target_ms + (half + 1) * 86400000)
        before_cands = list_candidates(lat, lng, b_start, b_end)
        before = choose_before(before_cands, before_target_ms, config.SAT_MIN_VALID_FRACTION)
        ok, reason = check_pair(before, after, config.SAT_MIN_SEPARATION_DAYS)
        if before is None:
            return {"status": "no_scene", "synthetic": False, "after_scene": after,
                    "message": f"No clean 'before' scene between {b_start} and {b_end} ({len(before_cands)} scene(s) found)."}
        if not ok:
            return {"status": "insufficient_separation", "synthetic": False, "before_scene": before, "after_scene": after,
                    "message": f"Rejected scene pair: {reason}."}

        metrics = None
        method = "ndwi_river_bed_mask_v2"
        try:
            metrics = compute_pair_metrics(lat, lng, before["id"], after["id"])
        except Exception as e:
            logger.warning(f"Masked metrics failed ({e}); trying plain buffer NDWI fallback")
        if metrics is None:
            metrics = _fallback_mean_metrics(lat, lng, before["id"], after["id"])
            method = "ndwi_mean_buffer_fallback"
        if metrics is None:
            return {"status": "no_scene", "synthetic": False, "before_scene": before, "after_scene": after,
                    "message": "Scenes found but no overlapping valid pixels inside the area of interest."}
        return {"status": "ok", "synthetic": False, "method": method, "before_scene": before, "after_scene": after,
                "message": f"Pair accepted: {reason}.", **metrics}
    except Exception as e:
        logger.warning(f"Satellite comparison failed at ({lat:.4f},{lng:.4f}): {e}")
        return {"status": "error", "synthetic": False, "message": f"Earth Engine request failed: {str(e)[:200]}"}


def _fallback_mean_metrics(lat, lng, before_id, after_id, buffer_m=1500):
    """Plain cloud-masked mean dNDWI over the buffer. Weaker (diluted), clearly labelled as fallback."""
    import ee
    b = ee.Image(f"{S2_COLLECTION}/{before_id}")
    a = ee.Image(f"{S2_COLLECTION}/{after_id}")
    _, aoi = _aoi(lat, lng, buffer_m)
    both = _valid_mask(b).And(_valid_mask(a))
    diff = a.normalizedDifference(["B3", "B8"]).subtract(b.normalizedDifference(["B3", "B8"]))
    res = ee.Dictionary({
        "mean": diff.updateMask(both).reduceRegion(ee.Reducer.mean(), aoi, 10, maxPixels=1e9, tileScale=4),
        "chg": diff.abs().gt(0.3).And(both).reduceRegion(ee.Reducer.mean(), aoi, 10, maxPixels=1e9, tileScale=4),
    }).getInfo()
    mean = next(iter(res["mean"].values()), None) if res.get("mean") else None
    chg = next(iter(res["chg"].values()), None) if res.get("chg") else None
    if mean is None or chg is None:
        return None
    return {"mask_source": "aoi_fallback_no_river_mask", "analysed_area_m2": None, "changed_fraction": round(float(chg), 4),
            "mean_ndwi_diff": round(float(mean), 4)}


def download_thumbnail(url, dest_path):
    """Downloads a GEE thumbnail URL to a local file. Returns True on success."""
    import requests
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        if "image" not in resp.headers.get("Content-Type", "image"):
            raise ValueError(f"unexpected content type {resp.headers.get('Content-Type')}")
        with open(dest_path, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        logger.error(f"Thumbnail download failed: {e}")
        return False
