"""
Geocoding — turns a place name ("Tungabhadra river Davangere") into real
coordinates, using OpenStreetMap's free Nominatim API. No API key needed,
but Nominatim's usage policy requires a real User-Agent and asks for max
~1 request/second, which is more than enough for a search-as-you-type box.
"""
import logging
import re
import requests

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "RiverGuard-SandMiningDetection/1.0 (final year project)"}

WATERWAY_TYPES = {"river", "stream", "canal", "drain", "ditch", "water"}
CITY_TYPES = {"city", "town", "village", "suburb", "locality", "hamlet", "neighbourhood"}


def _query_variants(query):
    """Build progressively broader search strings when the exact query misses."""
    q = query.strip()
    variants = [q]
    stripped = re.sub(r"\b(the|river|rivulet|stream|nadi|nadiya)\b", " ", q, flags=re.I)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped and stripped.lower() != q.lower():
        variants.append(stripped)
    parts = [p for p in re.split(r"[,;]", q) if p.strip()]
    if len(parts) > 1:
        variants.append(parts[0].strip())
        variants.append(", ".join(parts[:2]).strip())
    words = q.split()
    if len(words) > 2:
        variants.append(" ".join(words[:2]))
    if len(words) > 1:
        variants.append(words[0])
    # de-dupe while preserving order
    seen = set()
    out = []
    for v in variants:
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _waterway_score(r):
    """Higher score = more likely an actual river/waterway, not a city."""
    score = 0
    cat = (r.get("category") or "").lower()
    typ = (r.get("type") or "").lower()
    cls = (r.get("class") or "").lower()
    display = (r.get("display_name") or "").lower()

    if cat == "waterway" or cls == "waterway":
        score += 120
    if typ in WATERWAY_TYPES:
        score += 80
    if "river" in display or "nadi" in display:
        score += 25
    if typ in CITY_TYPES:
        score -= 90
    if cat == "place" and typ in CITY_TYPES:
        score -= 40
    return score


def _nominatim_search(query, limit=8):
    resp = requests.get(
        NOMINATIM_URL,
        params={
            "q": query,
            "format": "jsonv2",
            "limit": limit,
            "addressdetails": 1,
            "dedupe": 1,
        },
        headers=HEADERS,
        timeout=12,
    )
    resp.raise_for_status()
    return resp.json()


def _to_candidate(r, fallback_name):
    address = r.get("address", {})
    name = r.get("name") or address.get("waterway") or address.get("river") or fallback_name
    return {
        "name": name,
        "display_name": r.get("display_name"),
        "lat": float(r["lat"]),
        "lng": float(r["lon"]),
        "type": r.get("type") or r.get("category"),
        "state": address.get("state"),
        "district": address.get("state_district") or address.get("county") or address.get("city"),
        "_score": _waterway_score(r),
    }


def search_place(query, limit=5):
    """
    Returns a list of candidate locations: [{name, display_name, lat, lng, type}, ...]
    Waterways are ranked above cities/towns so "Tunga" picks the river, not the town center.
    """
    if not query or len(query.strip()) < 2:
        return []
    try:
        seen = set()
        candidates = []
        for variant in _query_variants(query):
            results = _nominatim_search(variant, limit=limit * 2)
            for r in results:
                key = (round(float(r["lat"]), 4), round(float(r["lon"]), 4), r.get("display_name"))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(_to_candidate(r, variant))
            if candidates:
                break

        candidates.sort(key=lambda c: c.get("_score", 0), reverse=True)
        final = []
        for c in candidates[:limit]:
            c = dict(c)
            c.pop("_score", None)
            final.append(c)
        return final
    except Exception as e:
        logger.error(f"Geocoding failed for '{query}': {e}")
        return []
