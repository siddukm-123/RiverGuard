"""
Evidence PDF generator — builds a tamper-evident incident report from an
alert: severity, location, reasoning text, detection list, and (if present)
before/after or annotated images. Every generated PDF is SHA-256 hashed so
the hash can be logged alongside it as a basic integrity check.
"""
import hashlib
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

import config


def _pdf_safe(text):
    """FPDF Helvetica only supports latin-1 — strip/replace unsupported Unicode."""
    if not text:
        return ""
    text = str(text)
    replacements = {
        "\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u2192": "->",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", errors="replace").decode("latin-1")


class EvidencePDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 16)
        self.set_text_color(20, 60, 90)
        self.cell(0, 10, "RiverGuard - Incident Evidence Report", ln=True)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(100, 100, 100)
        self.cell(0, 6, f"Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}", ln=True)
        self.ln(4)
        self.set_draw_color(200, 200, 200)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(6)


def generate_evidence_pdf(alert, river_name, detections=None, image_paths=None):
    """
    alert: dict (as stored in the `alerts` table).
    detections: optional list of detection dicts to list in the report.
    image_paths: optional list of local file paths to embed (before/after or annotated frame).
    Returns (pdf_path, sha256_hash).
    """
    pdf = EvidencePDF()
    pdf.add_page()

    severity_colors = {"HIGH": (200, 40, 40), "WATCH": (200, 140, 20), "SAFE": (30, 140, 60)}
    color = severity_colors.get(alert["severity"], (80, 80, 80))

    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*color)
    pdf.cell(0, 8, f"Severity: {alert['severity']}  (confidence {alert.get('confidence', 0)}%)", ln=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    pdf.set_font("Helvetica", "", 11)
    fields = [
        ("River", river_name),
        ("Location", f"{alert.get('lat')}, {alert.get('lng')}"),
        ("Inside legal buffer zone", str(alert.get("inside_buffer"))),
        ("Source", alert.get("source", "-")),
        ("Detected at", str(alert.get("detected_at", "-"))),
        ("Alert ID", str(alert.get("id", "-"))),
    ]
    for label, value in fields:
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(55, 7, f"{label}:")
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 7, _pdf_safe(str(value)), ln=True)

    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "AI Reasoning", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, _pdf_safe(alert.get("reasoning_text", "No reasoning text available.")))
    pdf.ln(3)

    if detections:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, f"Detections ({len(detections)})", ln=True)
        pdf.set_font("Helvetica", "", 9)
        for d in detections[:25]:
            line = f"- {d['class_name']} (conf {d['confidence']:.2f})"
            if d.get("track_id") is not None:
                line += f", track #{d['track_id']}"
            pdf.cell(0, 5, line, ln=True)
        if len(detections) > 25:
            pdf.cell(0, 5, f"... and {len(detections) - 25} more", ln=True)
        pdf.ln(3)

    if image_paths:
        for img_path in image_paths:
            if Path(img_path).exists():
                try:
                    pdf.image(str(img_path), w=180)
                    pdf.ln(3)
                except Exception:
                    pass

    config.EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.EVIDENCE_DIR / f"evidence_{alert['id']}.pdf"
    pdf.output(str(out_path))

    sha256 = hashlib.sha256(out_path.read_bytes()).hexdigest()
    return str(out_path), sha256
