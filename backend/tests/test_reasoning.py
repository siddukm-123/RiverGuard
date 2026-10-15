import config
from detection import reasoning as R


def base(**kw):
    s = {"input_type": "image", "inside_buffer": None, "best_cluster": None, "track_behaviors": [], "object_counts": {}}
    s.update(kw)
    return s


def cluster(**kw):
    c = {"has_truck": False, "has_jcb": False, "has_person": False, "count": 1, "avg_confidence": 0.8}
    c.update(kw)
    return c


def test_location_alone_is_not_evidence():
    """REGRESSION: any upload located in the buffer used to be WATCH (35 pts) even with zero detections."""
    r = R.score_signals(base(inside_buffer=True))
    assert r["score"] == 0 and r["severity"] == "SAFE"


def test_buffer_counts_only_with_equipment():
    r = R.score_signals(base(inside_buffer=True, best_cluster=cluster(has_truck=True), object_counts={"truck": 1}))
    assert r["score"] == 35 + 15 and r["severity"] == "WATCH"


def test_paper_table_weights_unchanged_for_manual_tier():
    r = R.score_signals(base(inside_buffer=True, best_cluster=cluster(has_truck=True, has_jcb=True, has_person=True, count=3),
                             object_counts={"truck": 1, "jcb": 1, "person": 1}))
    assert r["score"] == 35 + 30 + 5 + 10 and r["severity"] == "HIGH"


def test_high_requires_excavator():
    """Truck + person + cluster>=3 + loitering inside buffer reached 80 (HIGH) with no excavator; now capped at WATCH."""
    sig = base(inside_buffer=True, best_cluster=cluster(has_truck=True, has_person=True, count=3), object_counts={"truck": 2, "person": 1},
               track_behaviors=[{"class_name": "truck", "behavior": "loitering"}])
    r = R.score_signals(sig)
    assert r["severity"] == "WATCH" and r["score"] == 64 and r["caps"]


def test_high_cap_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "HIGH_REQUIRES_CORROBORATION", False)
    sig = base(inside_buffer=True, best_cluster=cluster(has_truck=True, has_person=True, count=3), object_counts={"truck": 2},
               track_behaviors=[{"class_name": "truck", "behavior": "loitering"}])
    assert R.score_signals(sig)["severity"] == "HIGH"


def test_satellite_never_reaches_high_and_has_no_free_buffer_points():
    for s in (0.0, 0.3, 0.6, 0.8, 1.0):
        r = R.score_signals({"input_type": "satellite", "satellite_change_score": s})
        assert r["score"] <= R.SATELLITE_ONLY_CAP and r["severity"] != "HIGH"
    assert R.score_signals({"input_type": "satellite", "satellite_change_score": 0.0})["severity"] == "SAFE"  # was WATCH(35)


def test_satellite_rain_confounder_reduces_score():
    a = R.score_signals({"input_type": "satellite", "satellite_change_score": 0.8})["score"]
    b = R.score_signals({"input_type": "satellite", "satellite_change_score": 0.8, "rain_confounder": True})["score"]
    assert b == a - 15


def test_synthetic_is_labelled_in_reasoning():
    r = R.score_signals({"input_type": "satellite", "satellite_change_score": 0.6, "synthetic_data": True})
    assert any("SYNTHETIC" in x for x in r["reasons"])


def test_no_data_satellite_scores_zero():
    assert R.score_signals({"input_type": "satellite", "satellite_change_score": None})["score"] == 0


def test_evaluate_backward_compatible_and_text_labelled():
    sev, score, text = R.evaluate(base())
    assert sev == "SAFE" and "rule_based" in text and "not proof" in text


def test_llm_cannot_change_severity(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(R, "_llm_reword", lambda result, signals: "Everything is HIGH and terrible.")
    r = R.evaluate_detailed(base())
    assert r["severity"] == "SAFE" and "LLM-worded" in r["method"]


def test_llm_failure_falls_back(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "x")
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(R, "_llm_reword", boom)
    assert R.evaluate_detailed(base())["method"] == "rule_based"
