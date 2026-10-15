"""
Reasoning engine - turns structured signals into severity, a 0-100 risk score and an explanation.

Design rules
  * The deterministic, additive rule scorer is the ONLY source of severity and score.
  * An optional LLM may re-WORD the explanation of that already-decided result. It cannot change
    severity/score and is told not to add facts. Any failure falls back to the rule text.
  * Every result records its `method` so the UI / PDF never mislabel rule output as "AI".
  * The score is a hand-set prior, NOT a calibrated probability (the DB column is still called
    `confidence` for backward compatibility; the UI calls it "risk score").
  * A detection is a signal for investigation, not proof of illegal mining.

Manual (image/video) weights are unchanged from the paper's Table I. What changed, and why:
  1. 'Inside buffer' (+35) only counts when truck/excavator evidence exists. Previously ANY upload
     whose location fell in the buffer (including the river centre filled in by default) was WATCH
     even with zero detections, and notified officers.
  2. HIGH needs corroboration (excavator detected). Trucks and people alone cap at WATCH.
  3. The satellite tier no longer gets the free +35 buffer term (its points are on the river by
     construction, so that term carried no information). Satellite-only evidence is scored on the
     change metrics and is capped below HIGH by design: it flags places to inspect, not mining.
"""
import json
import logging

import config

logger = logging.getLogger(__name__)

SEVERITIES = ("HIGH", "WATCH", "SAFE")
HIGH_THRESHOLD = 65
WATCH_THRESHOLD = 30
SATELLITE_ONLY_CAP = 55  # < HIGH_THRESHOLD by design


def _severity_for(score):
    if score >= HIGH_THRESHOLD:
        return "HIGH"
    if score >= WATCH_THRESHOLD:
        return "WATCH"
    return "SAFE"


def _add(breakdown, label, points, applied, note=""):
    breakdown.append({"signal": label, "points": points if applied else 0, "applied": bool(applied), "note": note})


def _satellite_points(sat_score):
    """Piecewise points from the corridor-relative change score (0-1)."""
    if sat_score is None or sat_score < 0.2:
        return 0
    if sat_score < 0.5:
        return 20
    if sat_score < 0.75:
        return 35
    return 50


def score_signals(signals):
    """Pure deterministic scorer. Returns dict(score, severity, reasons, breakdown, caps, tier)."""
    score = 0
    reasons, breakdown, caps = [], [], []
    tier = signals.get("input_type", "image")

    if tier == "satellite":
        sat = signals.get("satellite_change_score")
        pts = _satellite_points(sat)
        if signals.get("synthetic_data"):
            reasons.append("SYNTHETIC DEMO DATA - no real satellite observation was used")
        if sat is None:
            reasons.append("no valid satellite comparison was available")
        else:
            _add(breakdown, "corridor-relative NDWI change", pts, pts > 0,
                 f"change score {sat:.2f} (0-1); thresholds 0.2/0.5/0.75 -> 20/35/50 pts")
            score += pts
            if pts:
                reasons.append(f"the vegetation/water-adjusted NDWI change at the hotspot is {sat:.0%} of the scale after removing river-wide change")
            else:
                reasons.append(f"the NDWI change at the hotspot ({sat:.0%}) is within normal variation")
        if signals.get("rain_confounder"):
            before = score
            score = max(0, score - 15)
            _add(breakdown, "heavy rainfall between scenes (natural confounder)", -(before - score), before != score)
            reasons.append("significant rainfall fell between the two scenes, so a natural hydrological change is a plausible explanation")
        if score > SATELLITE_ONLY_CAP:
            score = SATELLITE_ONLY_CAP
        caps.append("satellite-only evidence can never reach HIGH; field/ground evidence is required")
    else:
        counts = signals.get("object_counts") or {}
        cluster = signals.get("best_cluster") or {}
        has_truck = bool(cluster.get("has_truck")) or counts.get("truck", 0) > 0
        has_jcb = bool(cluster.get("has_jcb")) or counts.get("jcb", 0) > 0
        heavy = has_truck or has_jcb

        buf = signals.get("inside_buffer")
        if buf is True and heavy:
            score += 35
            _add(breakdown, "inside protected buffer (with equipment present)", 35, True)
            reasons.append("heavy equipment was detected at a location inside the protected river buffer zone")
        elif buf is True:
            _add(breakdown, "inside protected buffer", 35, False, "no truck/excavator detected - location alone is not evidence")
            reasons.append("the location is inside the buffer zone, but no truck or excavator was detected, so location alone adds no risk")
        elif buf is False:
            reasons.append("the location is outside the protected buffer zone")
        else:
            reasons.append("zone status is unknown (no reliable GPS location was supplied)")
        if signals.get("location_source") == "river_default":
            reasons.append("the location is the river's registered centre, not a surveyed position of this footage")

        if cluster:
            if cluster.get("has_truck") and cluster.get("has_jcb"):
                score += 30
                _add(breakdown, "truck + excavator in the same cluster", 30, True)
                reasons.append("a truck and an excavator (JCB) were detected together at the same spot")
            elif cluster.get("has_truck"):
                score += 15
                _add(breakdown, "truck in cluster (no excavator)", 15, True)
                reasons.append("a truck was detected at the site")
            elif cluster.get("has_jcb"):
                score += 20
                _add(breakdown, "excavator in cluster (no truck)", 20, True, "added: an excavator alone is stronger evidence than a person")
                reasons.append("an excavator (JCB) was detected at the site")
            if cluster.get("has_person"):
                score += 5
                _add(breakdown, "person in cluster", 5, True)
                reasons.append("people were present at the site")
            if cluster.get("count", 0) >= 3:
                score += 10
                _add(breakdown, "cluster of >= 3 distinct objects", 10, True)
                reasons.append(f"{cluster['count']} distinct objects were clustered in one area, suggesting an active work site")
        elif heavy:
            # equipment seen but not spatially grouped with anything
            pts = 15 if has_truck and not has_jcb else 20
            score += pts
            _add(breakdown, "isolated equipment detection", pts, True)
            reasons.append("equipment was detected but not grouped with other objects")

        loiter = [b for b in signals.get("track_behaviors", []) if b["class_name"] == "truck" and b["behavior"] == "loitering"]
        if loiter:
            score += 15
            _add(breakdown, "loitering truck (video)", 15, True)
            reasons.append(f"{len(loiter)} truck(s) stayed in place for an extended period, consistent with loading")

        score = min(100, score)
        if config.HIGH_REQUIRES_CORROBORATION and score >= HIGH_THRESHOLD and not has_jcb:
            score = HIGH_THRESHOLD - 1
            caps.append("capped at WATCH: no excavator detected, and trucks/people alone are not enough for HIGH")
            reasons.append("severity was capped at WATCH because no excavation equipment was detected")
        if not has_jcb and not (signals.get("model_info") or {}).get("excavator_detection_available", False):
            caps.append("excavator detection is not available with the loaded model (COCO has no excavator class)")

    score = int(max(0, min(100, score)))
    if not reasons:
        reasons.append("no significant risk signals were found in this input")
    return {"score": score, "severity": _severity_for(score), "reasons": reasons, "breakdown": breakdown, "caps": caps, "tier": tier}


def _cap1(t):
    return t[:1].upper() + t[1:]


def _compose_text(result, method):
    text = f"Assessed as {result['severity']} (risk score {result['score']}/100, {method}). " + _cap1("; ".join(result["reasons"])) + "."
    if result["caps"]:
        text += " Notes: " + "; ".join(result["caps"]) + "."
    text += " This is a screening signal for investigation, not proof of illegal activity."
    return text


def _llm_reword(result, signals):
    """Ask the LLM to explain the ALREADY DECIDED result. It cannot change severity or score."""
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    safe = {k: v for k, v in signals.items() if k not in ("scan_results", "media", "provenance", "model_info")}
    prompt = (
        "You are writing the explanation section of a river-monitoring alert. The severity and score below were "
        "ALREADY computed by a deterministic rule engine; do not change or second-guess them, and do not add any fact "
        "that is not in the data. Write 2-4 plain-English sentences a field supervisor can read quickly. State clearly "
        "that this is a screening signal, not proof. If the data is synthetic, say so.\n\n"
        f"Severity: {result['severity']}\nRisk score: {result['score']}/100\n"
        f"Rule reasons: {json.dumps(result['reasons'])}\nCaps/notes: {json.dumps(result['caps'])}\n"
        f"Structured signals: {json.dumps(safe, default=str)[:3000]}\n\n"
        "Reply with the explanation text only."
    )
    resp = client.messages.create(model=config.REASONING_MODEL, max_tokens=300, messages=[{"role": "user", "content": prompt}])
    text = resp.content[0].text.strip()
    if not text or len(text) > 1500:
        raise ValueError("LLM returned an empty or oversized explanation")
    return text


def evaluate_detailed(signals):
    result = score_signals(signals)
    method = "rule_based"
    text = _compose_text(result, method)
    if config.ANTHROPIC_API_KEY:
        try:
            llm_text = _llm_reword(result, signals)
            method = f"rule_based + LLM-worded explanation ({config.REASONING_MODEL})"
            text = f"Assessed as {result['severity']} (risk score {result['score']}/100, {method}). {llm_text}"
        except Exception as e:
            logger.warning(f"LLM explanation failed, using rule-based text: {e}")
    return {**result, "method": method, "text": text}


def evaluate(signals):
    """Backwards-compatible: returns (severity, risk_score, reasoning_text)."""
    r = evaluate_detailed(signals)
    return r["severity"], r["score"], r["text"]
