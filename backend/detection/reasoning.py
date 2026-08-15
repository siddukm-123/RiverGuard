"""
Reasoning engine — the "human inspector" layer.

Takes structured signals (cluster composition, track behavior, zone status,
satellite change score) and produces:
  - severity: HIGH / WATCH / SAFE
  - confidence: 0-100
  - reasoning_text: a plain-English explanation, written the way a human
    inspector would justify a call

Works in two modes:
  - If ANTHROPIC_API_KEY is set, calls the Claude API to write the actual
    judgment/explanation from the structured signals.
  - If not, falls back to a deterministic rule-based scorer so the whole
    project still runs and demos correctly with zero API keys configured.
"""
import json
import logging

import config

logger = logging.getLogger(__name__)


def _rule_based_reasoning(signals):
    score = 0
    reasons = []

    if signals.get("inside_buffer") is True:
        score += 35
        reasons.append("activity is located inside the protected river buffer zone")
    elif signals.get("inside_buffer") is None:
        reasons.append("zone status could not be confirmed (no GPS/river reference attached)")

    cluster = signals.get("best_cluster")
    if cluster:
        if cluster.get("has_truck") and cluster.get("has_jcb"):
            score += 30
            reasons.append("a truck and an excavator (JCB) were detected together at the same spot")
        elif cluster.get("has_truck"):
            score += 15
            reasons.append("a truck was detected at the site")
        if cluster.get("has_person"):
            score += 5
            reasons.append("people were present at the site")
        if cluster.get("count", 0) >= 3:
            score += 10
            reasons.append(f"{cluster['count']} objects were detected clustered in one area, suggesting an active work site")

    behaviors = signals.get("track_behaviors", [])
    loitering_trucks = [b for b in behaviors if b["class_name"] == "truck" and b["behavior"] == "loitering"]
    if loitering_trucks:
        score += 15
        reasons.append(f"{len(loitering_trucks)} truck(s) stayed stationary at the site for an extended period, consistent with loading")

    sat_score = signals.get("satellite_change_score")
    if sat_score is not None and sat_score > 0.5:
        score += 20
        reasons.append(f"satellite comparison shows a sandbar/riverbed area change of {sat_score:.0%}")

    score = min(100, score)
    if score >= 65:
        severity = "HIGH"
    elif score >= 30:
        severity = "WATCH"
    else:
        severity = "SAFE"

    if not reasons:
        reasons.append("no significant risk signals were found in this input")

    text = f"Assessed as {severity} ({score}/100). " + "; ".join(reasons).capitalize() + "."
    return severity, score, text


def _llm_reasoning(signals):
    """Calls the Claude API to produce a richer, human-style judgment."""
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""You are an experienced river-enforcement field inspector reviewing automated detection data for a possible illegal sand-mining incident. Reason through the evidence like a careful human inspector would — consider alternative explanations (legal transport, construction, fishing, seasonal flooding) before concluding.

Structured signals detected by the system:
{json.dumps(signals, indent=2, default=str)}

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"severity": "HIGH" | "WATCH" | "SAFE", "confidence": <0-100 integer>, "reasoning": "<2-4 sentence plain-English explanation of your judgment, written the way you'd justify it to a supervisor>"}}"""

    response = client.messages.create(
        model=config.REASONING_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw)
    return parsed["severity"], int(parsed["confidence"]), parsed["reasoning"]


def evaluate(signals):
    """
    signals: dict, e.g.
    {
        "inside_buffer": True/False/None,
        "best_cluster": {...from cluster_engine.summarize_clusters...},
        "track_behaviors": [...from tracker.finalize...],
        "satellite_change_score": 0.0-1.0 or None,
        "time_of_day": "night"/"day"/None,
    }
    Returns (severity, confidence, reasoning_text). Always falls back to the
    rule-based scorer if the LLM call fails for any reason, so a missing/
    invalid API key never breaks the pipeline.
    """
    if config.ANTHROPIC_API_KEY:
        try:
            return _llm_reasoning(signals)
        except Exception as e:
            logger.warning(f"LLM reasoning failed, falling back to rule-based: {e}")

    return _rule_based_reasoning(signals)
