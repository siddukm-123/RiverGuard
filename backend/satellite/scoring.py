"""
Pure-python (no Earth Engine) scoring of per-point satellite metrics, so it can be unit-tested.

Why a corridor-relative score?
  Rain, discharge and season change the water extent of the WHOLE river reach between two dates.
  A local excavation changes one stretch much more than its neighbours. So each point's changed
  fraction is compared with the MEDIAN changed fraction of all points scanned on the same river
  ("background"); only the excess counts. With fewer than 3 usable points there is no reliable
  background, the raw fraction is used with a stricter scale, and the result says so.

The 0.25 / 0.5 scale constants are uncalibrated priors (no labelled mining dataset exists yet).
"""
from statistics import median

EXCESS_FOR_FULL_SCORE = 0.25   # excess changed-area fraction (of river-bed area) that maps to score 1.0
RAW_FOR_FULL_SCORE = 0.50      # same, when no background could be estimated
MIN_POINTS_FOR_BACKGROUND = 3


def corridor_scores(point_results):
    """
    point_results: list of dicts from gee_client.get_before_after_pair (mutated in place).
    Adds: background_changed_fraction, excess_change, change_score, background_available.
    Points whose status != 'ok' get change_score None.
    """
    ok = [r for r in point_results if r.get("status") == "ok" and r.get("changed_fraction") is not None]
    have_bg = len(ok) >= MIN_POINTS_FOR_BACKGROUND
    bg = median(r["changed_fraction"] for r in ok) if have_bg else 0.0
    for r in point_results:
        if r not in ok:
            r["change_score"] = None
            r["excess_change"] = None
            r["background_changed_fraction"] = None
            r["background_available"] = have_bg
            continue
        excess = max(0.0, r["changed_fraction"] - bg)
        full = EXCESS_FOR_FULL_SCORE if have_bg else RAW_FOR_FULL_SCORE
        r["background_changed_fraction"] = round(bg, 4)
        r["background_available"] = have_bg
        r["excess_change"] = round(excess, 4)
        r["change_score"] = round(min(1.0, excess / full), 3)
    return point_results


def pick_hotspot(point_results):
    """Highest change_score among usable points, or None when no point has a valid comparison."""
    usable = [r for r in point_results if r.get("change_score") is not None]
    return max(usable, key=lambda r: r["change_score"]) if usable else None
