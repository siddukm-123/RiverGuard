"""
Evidence PDF generator.

Every number in the report comes from the stored alert/pipeline output - nothing is typed in.
The PDF is hashed with SHA-256 over the exact bytes written to disk; the digest is stored with the
alert and can be re-checked via GET /api/alerts/<id>/evidence/verify.

A SHA-256 proves the file was not modified AFTER generation. It does not prove the observation is
true, authentic, or legally admissible, and it is not a chain of custody.
"""
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF

import config


def _pdf_safe(text):
    """Core PDF fonts are latin-1 only - replace the common typographic characters, then drop the rest."""
    if text is None:
        return ""
    text = str(text)
    for src, dst in {"\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                     "\u2026": "...", "\u2192": "->", "\u2265": ">=", "\u2264": "<=", "\u00b1": "+/-", "\u2248": "~"}.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", errors="replace").decode("latin-1")


class EvidencePDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 16)
        self.set_text_color(20, 60, 90)
        self.cell(0, 10, "RiverGuard - Incident Evidence Report", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(100, 100, 100)
        self.cell(0, 6, f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)
        self.set_draw_color(200, 200, 200)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 5, f"Screening signal for investigation - not proof of illegal activity. Page {self.page_no()}", align="C")


def _h(pdf, text):
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(20, 60, 90)
    pdf.cell(0, 7, _pdf_safe(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)


def _kv(pdf, label, value, w=58):
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(w, 6, _pdf_safe(f"{label}:"))
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, _pdf_safe(value), new_x="LMARGIN", new_y="NEXT")


def _para(pdf, text, size=10):
    pdf.set_font("Helvetica", "", size)
    pdf.multi_cell(0, 5.5, _pdf_safe(text), new_x="LMARGIN", new_y="NEXT")


def generate_evidence_pdf(alert, river_name, detections=None, image_paths=None, extra=None):
    """
    alert: stored alert dict. detections: list of detection dicts. image_paths: local images to embed.
    extra: the alert's `reasoning_signals` dict (provenance, model info, low-light, satellite scenes, breakdown).
    Returns (pdf_path, sha256_hex).
    """
    extra = extra or {}
    pdf = EvidencePDF()
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()

    is_sat = alert.get("source") == "satellite"
    synthetic = bool(extra.get("synthetic_data"))
    if synthetic:
        pdf.set_fill_color(255, 230, 200)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(160, 60, 0)
        pdf.multi_cell(0, 7, "SYNTHETIC DEMONSTRATION DATA - NOT A REAL SATELLITE OBSERVATION", fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    colors = {"HIGH": (200, 40, 40), "WATCH": (200, 140, 20), "SAFE": (30, 140, 60)}
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*colors.get(alert["severity"], (80, 80, 80)))
    pdf.cell(0, 8, f"Severity: {alert['severity']}   |   Risk score {alert.get('confidence', 0)}/100", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    _para(pdf, "The risk score is a hand-set, rule-based prior. It is not a calibrated probability and has not been validated against labelled mining events.", 8)

    _h(pdf, "Alert")
    inside = alert.get("inside_buffer")
    if is_sat:
        zone = "n/a - satellite points are placed on the river corridor by construction"
    else:
        zone = {True: "Yes", False: "No", None: "Unknown"}[inside] + (f" ({extra.get('zone_method')})" if extra.get("zone_method") else "")
    loc_note = {"river_default": " (river's registered centre - NOT a surveyed position of this footage)",
                "operator_supplied": " (operator supplied)"}.get(extra.get("location_source"), "")
    for k, v in [("Alert ID", alert.get("id", "-")), ("River", river_name), ("Source", alert.get("source", "-")),
                 ("Location", f"{alert.get('lat')}, {alert.get('lng')}{loc_note}"), ("Inside buffer zone", zone),
                 ("Detected at", alert.get("detected_at", "-"))]:
        _kv(pdf, k, v)

    _h(pdf, "Assessment")
    rs = extra.get("reasoning") or {}
    _kv(pdf, "Reasoning method", rs.get("method", "rule_based"))
    _para(pdf, alert.get("reasoning_text") or "No reasoning text available.")
    if rs.get("breakdown"):
        pdf.ln(1)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Score breakdown", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for b in rs["breakdown"]:
            pts = f"{b['points']:+d}" if b.get("applied") else "0 (not applied)"
            pdf.multi_cell(0, 5, _pdf_safe(f"  {pts:>16}  {b['signal']}" + (f" - {b['note']}" if b.get("note") else "")), new_x="LMARGIN", new_y="NEXT")
    for c in rs.get("caps", []):
        _para(pdf, f"Note: {c}", 9)

    if is_sat:
        _h(pdf, "Satellite observation")
        bs, af = extra.get("before_scene") or {}, extra.get("after_scene") or {}
        if synthetic:
            _para(pdf, "No Sentinel-2 scene was used. The change value shown on the dashboard is a synthetic placeholder.")
        else:
            _kv(pdf, "Data source", "Sentinel-2 SR Harmonized (COPERNICUS/S2_SR_HARMONIZED) via Google Earth Engine")
            _kv(pdf, "Before scene", f"{bs.get('date')}  id {bs.get('id')}  clean fraction {bs.get('valid', 0):.0%}")
            _kv(pdf, "After scene", f"{af.get('date')}  id {af.get('id')}  clean fraction {af.get('valid', 0):.0%}")
            _kv(pdf, "Method", f"{extra.get('method')}  |  mask: {extra.get('mask_source')}")
            m = extra.get("metrics") or {}
            _kv(pdf, "Hotspot", f"{extra.get('hotspot_label')}  |  changed bed-area fraction {extra.get('changed_fraction')}  |  excess over river-wide background {extra.get('excess_change')}")
            _kv(pdf, "Mean dNDWI (after-before)", extra.get("raw_ndwi_diff"))
            if m.get("analysed_area_m2"):
                _kv(pdf, "Area analysed (m2)", f"{m['analysed_area_m2']}  |  water {m.get('water_before_m2')} -> {m.get('water_after_m2')}  |  non-water/non-vegetated {m.get('bare_before_m2')} -> {m.get('bare_after_m2')}")
            _kv(pdf, "Corridor points", f"{extra.get('corridor_points_usable')} usable of {extra.get('corridor_points_scanned')}  |  river-wide background removed: {extra.get('background_available')}")
            _kv(pdf, "Rainfall between scenes", f"{extra.get('rain_mm_between_scenes')} mm" if extra.get("rain_mm_between_scenes") is not None else "unavailable")
            _para(pdf, "Limitations: a spectral index change cannot by itself separate excavation from flood recession, seasonal flow, "
                       "sediment movement or acquisition differences. Treat as a place to inspect, not a finding.", 9)
    else:
        mi = extra.get("model_info") or {}
        prov = extra.get("provenance") or {}
        _h(pdf, "Detection provenance")
        _kv(pdf, "Source file", f"{prov.get('source_name')}  ({prov.get('source_bytes')} bytes)")
        _kv(pdf, "Source SHA-256", prov.get("source_sha256"))
        _kv(pdf, "Model", f"{mi.get('base_model')} (ultralytics {mi.get('framework_version')}), conf >= {mi.get('conf_threshold')}")
        _kv(pdf, "Weights SHA-256", mi.get("base_model_sha256") or "not recorded")
        _kv(pdf, "Excavator detection", "available (custom model: %s)" % mi.get("custom_model") if mi.get("excavator_detection_available")
            else "NOT available - the loaded model has no excavator/JCB class")
        oc = extra.get("object_counts") or {}
        _kv(pdf, "Evidence objects", ", ".join(f"{k}: {v}" for k, v in oc.items()) or "none")
        if extra.get("video"):
            v = extra["video"]
            _kv(pdf, "Video", f"{v.get('duration_s')} s @ {v.get('video_fps')} fps, {v.get('frames_analyzed')} frames analysed"
                              + ("  (TRUNCATED at max length)" if v.get("truncated") else ""))
        ll = extra.get("lowlight") or {}
        if ll.get("applied") or (ll.get("frames_enhanced") or 0) > 0:
            _h(pdf, "Low-light processing")
            _para(pdf, "Software contrast enhancement (denoise, gamma, CLAHE) was applied. This is NOT thermal/infrared night vision and cannot "
                       "recover detail the sensor never captured. Original and enhanced detections are labelled separately.", 9)
            cmp_ = extra.get("lowlight_comparison") or {}
            if cmp_:
                _kv(pdf, "Detections (original)", cmp_.get("original_counts"))
                _kv(pdf, "Detections (enhanced)", cmp_.get("enhanced_counts"))

    if detections:
        _h(pdf, f"Detections ({len(detections)})")
        pdf.set_font("Helvetica", "", 8.5)
        for d in detections[:40]:
            line = f"- {d['class_name']} conf {d['confidence']:.2f}"
            if d.get("track_id") is not None:
                line += f" track #{d['track_id']}"
            if d.get("timestamp_s") is not None:
                line += f" t={d['timestamp_s']}s"
            if d.get("view"):
                line += f" [{d['view']}]"
            if d.get("role") == "context":
                line += " (context only)"
            pdf.cell(0, 4.8, _pdf_safe(line), new_x="LMARGIN", new_y="NEXT")
        if len(detections) > 40:
            pdf.cell(0, 5, f"... and {len(detections) - 40} more (full list in the database)", new_x="LMARGIN", new_y="NEXT")

    if image_paths:
        for img in image_paths:
            if Path(img).exists():
                try:
                    pdf.ln(3)
                    pdf.image(str(img), w=180)
                except Exception:
                    pass

    _h(pdf, "Integrity")
    _para(pdf, "The SHA-256 of this PDF is stored with the alert (not inside the PDF). Recompute it over the downloaded file to detect later "
               "modification. A hash does not prove the underlying observation is true or authentic.", 9)

    config.EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.EVIDENCE_DIR / f"evidence_{alert['id']}.pdf"
    pdf.output(str(out_path))
    return str(out_path), hashlib.sha256(out_path.read_bytes()).hexdigest()
