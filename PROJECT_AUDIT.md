# RiverGuard — Project Audit & Research Traceability

Basis: the uploaded repository + `RiverGuard_IJCRT_Format.docx`. Every finding below was read in code; fixes were run (see `TEST_REPORT.md`).

## 1. Dependency map
Browser (Vercel static) → `/api/*` rewrite → Flask (Railway) → `routes.py` → `pipeline.py` →
[`detector` (YOLO) → `tracker` → `cluster_engine` → `zone_checker` → `reasoning`] or [`gee_client` → `scoring` → `reasoning`] →
`pdf_generator` (SHA-256) → `supabase_client` (Postgres / in-memory fallback) → `notifier` (Twilio/SMTP). `scheduler.py` re-runs satellite scans; `jobs.py` (new) runs slow work off-request.

## 2. What was genuinely implemented (kept)
Flask API + static dashboards; YOLOv8 inference (person/truck); OSM corridor sampling; Nominatim search; DBSCAN; additive scorer (Table I); PDF + SHA-256; Supabase wrapper with memory fallback; APScheduler; Railway/Vercel deployment design.

## 3. Defects found and fixed
| # | Defect (verified in code) | Impact | Fix |
|---|---|---|---|
| 1 | Satellite pairing: ±30-day windows → same scene as before/after (paper Table II) | ΔNDWI trivially 0 | `gee_client`: distinct scenes, ≥10 d apart, ~30 d baseline, else *no comparison* |
| 2 | Cloud filter = whole-tile `CLOUDY_PIXEL_PERCENTAGE` | cloudy AOI accepted | Per-pixel SCL mask; required clean fraction inside AOI |
| 3 | Metric = abs(mean ΔNDWI) over 1.5 km buffer (dilution, sign lost) | weak/uninterpretable | River-bed mask (JRC GSW), changed-area fraction, water / non-vegetated-bed areas |
| 4 | GEE configured but no scene → **random "synthetic" score → alert → officers notified** | false alerts | No numbers when GEE fails; synthetic only when credentials absent, labelled, never notified |
| 5 | No control for river-wide/seasonal change | flood ≠ mining | Corridor median background subtracted; CHIRPS rainfall flag |
| 6 | Satellite alerts got free +35 "inside buffer" | meaningless WATCH, max 55 | Removed; satellite-only capped < HIGH by design |
| 7 | Any upload with a river selected = WATCH even with 0 detections (river centre used as GPS → "inside buffer") + notification | alert spam | Buffer points need equipment evidence; river-default location ⇒ zone *unknown* |
| 8 | HIGH reachable with trucks+people, no excavator | over-claiming | HIGH requires excavator (configurable) |
| 9 | Tracker dwell = raw frame indices ÷ *sampling* fps (≈15× too long) | false "loitering truck" | Uses real video fps; loitering relative to object size, ≥3 observations |
| 10 | Clustering over all frames: one parked truck in 3 frames = "cluster of 3" | false work-site | Per-frame clustering, unique-track counting, resolution-scaled eps |
| 11 | Annotated video thumbnail drew boxes from ~30 *different frames* on one frame | misleading evidence | Only that frame's detections |
| 12 | COCO car/bus folded into "truck" | inflated truck signal | Context-only classes (legacy via `FOLD_VEHICLES_AS_TRUCK`) |
| 13 | Custom model: every class labelled "jcb" | wrong labels | Class-name mapping; unrelated classes stay context |
| 14 | `build_buffer_polygon` never called; hard-coded UTM 44N (wrong for most of India) | zone check = distance to one point | Polygon built at river add/refresh in the correct UTM zone; poly-line distance fallback |
| 15 | LLM could set severity/score (unvalidated → DB constraint error) | non-reproducible | Rules decide; LLM only re-words; `method` recorded |
| 16 | Night/low-light: nothing existed | — | `lowlight.py` + API + UI (see §5) |
| 17 | Unconfigured Twilio/SMTP wrote `failed` | looked like delivery error | Status stays `not_sent`; `/api/system-status` shows config |
| 18 | Satellite alerts re-notified every scan | spam | cooldown + duplicate-evidence suppression |
| 19 | Frontend `innerHTML` with unescaped server text/filenames | XSS | escaped |
| 20 | 413/500 returned HTML; inconsistent errors | UI JSON parse errors | JSON errors everywhere with `code` |
| 21 | 2–3 min scan held open through Vercel proxy | timeouts | async jobs (202 + polling) |
| 22 | No `.gitignore` / `.env.example`; `vercelignore` lacked the dot; no healthcheckPath; weights downloaded on first request; models dir not in image | deploy fragility | added/renamed/baked |
| 23 | No write protection | anyone can delete/scan | optional `RIVERGUARD_API_KEY` |
| 24 | Permissive RLS | direct DB access with anon key | optional SQL `0002` (service-role only) |
| 25 | 4 sequential DB inserts per detection | slow | batch insert |

## 4. Research-paper traceability
| Research requirement | Existing implementation | Gap | Corrective change | Verification |
|---|---|---|---|---|
| Sentinel-2 via GEE, cloud-filtered | `COPERNICUS/S2_SR_HARMONIZED`, tile-level cloud % | tile-level cloud; same-scene pairs | SCL masking, AOI clean-fraction, distinct bracketing scenes | pair logic unit-tested; **GEE calls not run (no credentials)** |
| NDWI (B3−B8)/(B3+B8) | correct formula | mean over diluted buffer | same formula, masked bed area, changed fraction (|ΔNDWI|>0.3) | formula review; mocked pipeline test |
| Before/after change detection | single-scene risk | see above | baseline ≥10 d, scenes + dates stored/shown | unit tests |
| Water / sand / exposed bed segmentation | none | missing | NDWI>0 water; NDWI≤0 & NDVI<0.3 = *candidate* non-vegetated bed (proxy, not validated) | **unvalidated, GEE-only** |
| Natural vs anthropogenic | none (paper §Limitations) | not modelled | corridor background + rainfall flag; documented as partial | unit tests |
| YOLO ground detection | real | car→truck, per-class labels | corrected classes, provenance, model status | real weights, real image/video |
| Tracking | IoU tracker | dwell bug | fixed + tests | real video test |
| DBSCAN clustering | pixel DBSCAN | cross-frame merge | per-frame | unit + video test |
| Buffer zones | centre-point distance | polygon never built, wrong UTM | built, correct UTM | unit tests |
| Risk score / alerts / evidence | additive scorer; PDF+SHA-256 | free buffer pts, HIGH w/o excavator; thin PDF | corrected; PDF has provenance, breakdown, scenes | tests + `/evidence/verify` |
| Dashboard integration | present | labels, honesty banners | status banner, risk-score wording, scene dates | browser test (detect page); main map **not browser-tested** (Leaflet CDN blocked in sandbox) |
| Excavator (JCB) detection | needs custom weights | none exist in repo | loader + class mapping + clear "NOT available" state; **no weights/dataset supplied → cannot be claimed** | status endpoint test |
| Reported accuracy | paper reports **none** | — | none hard-coded anywhere | grep |

## 5. Low-light
New `detection/lowlight.py`: brightness/noise/blur assessment; denoise → adaptive gamma → CLAHE; modes off/auto/force; overexposed images never brightened; original untouched; original+enhanced detections both kept and tagged (`view`), side-by-side comparison image; per-image counts only. **Not night vision**; no claim that it improves accuracy (needs labelled night data — `tools/evaluate_lowlight.py` prints counts only). Thermal/IR is a hardware requirement, not implemented.

## 6. Still open / unverified
Real GEE run; real Supabase/Twilio/SMTP; custom excavator weights & labelled benchmark; main dashboard map in a browser; large-video upload through the Vercel proxy; multi-worker job store.

## 7. Impact on the paper (please read)
The paper describes the *old* behaviour (Table I satellite +35, ±30-day pairing, 432-grid result 124/210/98, "five-day" comparison). The code now differs: satellite scoring, HIGH cap, no free buffer points, 30-day baseline. The paper's audit section remains a true record of v1; either cite this as v2 or update §Evaluation/Table I before submission.
