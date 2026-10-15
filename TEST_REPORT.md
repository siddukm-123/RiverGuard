# TEST REPORT
Environment: Python 3.12 sandbox, Flask 3.1, ultralytics 8.2.103, torch 2.14 (CUDA wheel, CPU run), **real `yolov8n.pt`** (SHA-256 f59b3d83…), no internet to Overpass/GEE/Supabase/Twilio/SMTP.

## Command
```
cd backend && YOLO_MODEL_PATH=/path/yolov8n.pt python -m pytest tests -q
```
Result: **55 passed, 0 skipped, 0 failed** (68 s). Without weights the 4 real-YOLO tests are reported as *skipped*, not passed.

## What ran for real
- YOLOv8n inference on ultralytics' bundled `bus.jpg`: 3 persons + 1 bus; bus stays `context`, never "truck".
- Full upload → detect → cluster → reason → annotated image → PDF → SHA-256 → `/evidence/verify` match.
- Dark image (×0.12) in `auto` mode: enhancement applied, comparison + enhanced images served, original/enhanced counts reported.
- Video (synthetic 6 s clip from the real photo): async job, tracking (3 people ⇒ 2–4 tracks, not one per frame), dwell ≤ real duration, annotated frame.
- Corrupt image/video, wrong magic bytes, bad extension, oversize (413), invalid JSON/dates: JSON errors with proper status.
- Real Chromium (Playwright): `/detect` page loads status, uploads, shows result + annotated image; `/details` lists rivers; zero JS errors / failed requests.
- Regression tests for defects #1, 7, 8, 9, 10, 12, 14.

## Mocked / not real
- Earth Engine: **mocked** (pipeline wiring/scoring/PDF verified; the `ee` calls themselves are UNTESTED).
- Notifications: dry-run/mock only. Supabase: in-memory only (persistence across restarts **not tested**).
- LLM reasoning: mocked.
- Main dashboard (`/`): Leaflet loads from unpkg, blocked in sandbox ⇒ **not browser-verified**; JS syntax checked with `node --check`.
- Overpass/Nominatim river search: not reachable ⇒ untested here (code unchanged except buffer building).
- Detection accuracy: **not measured** (no labelled data). Excavator detection: **not available**.

## Feature status
| Feature | Local real | Real external | Mocked/synthetic | Blocked |
|---|---|---|---|---|
| Image/video YOLO pipeline | ✅ | — | — | — |
| Low-light enhancement | ✅ | — | — | benefit unproven |
| Evidence PDF + hash | ✅ | — | — | — |
| Satellite scan | pipeline ✅ | ❌ | mocked / synthetic | needs GEE creds |
| Supabase persistence | — | ❌ | memory | needs creds |
| WhatsApp / e-mail | — | ❌ | dry-run | needs creds |
| Excavator detection | loader ✅ | — | — | needs weights |
