# RiverGuard

AI-powered illegal river-sand-mining detection. No drones, no hardware — two
input tiers, both pure software:

1. **Satellite tier** — scheduled Sentinel-2 before/after comparison via
   Google Earth Engine, flags sandbar/riverbed area change.
2. **Manual detection tier** — upload an image or video (a separate page in
   the dashboard) and it runs real YOLOv8 object detection + tracking +
   clustering + an AI reasoning layer that gives a human-style verdict.

Both tiers feed the same alerting pipeline: buffer-zone check → severity
scoring → WhatsApp/email notification to the mapped officer → PDF evidence
report → live dashboard.

See `PROJECT_AUDIT.md` (what was fixed and why) and `TEST_REPORT.md` (what was actually executed, what was mocked, what is unverified). Run tests: `cd backend && python -m pytest tests -q`.

New in v2: low-light enhancement (software only — not night vision), async jobs, evidence verify endpoint, corridor-relative satellite scoring, `/api/system-status`. A detection or satellite change is a screening signal, never proof of illegal mining. The one thing that needs
your own credentials to test is the satellite tier against real Earth Engine
data and the WhatsApp send — both work correctly with graceful fallbacks
when unconfigured (see below).

## Quick start (zero configuration)

The app runs immediately with **no API keys at all** — it falls back to
in-memory storage, rule-based reasoning, and synthetic satellite data, so
you can see the whole thing working before connecting anything real.

```bash
cd riverguard
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cd backend
python3 app.py
```

Open `http://localhost:8000` for the dashboard, `http://localhost:8000/detect`
for manual image/video detection.

The first run downloads YOLOv8's pretrained weights (~6MB) automatically.

## Connecting things for real

### 1. Supabase (persistent storage)

1. Create a project at [supabase.com](https://supabase.com).
2. Go to **SQL Editor**, paste the contents of
   `supabase/migrations/0001_init.sql`, and run it. This creates every table
   (`rivers`, `officers`, `alerts`, `detections`, `evidence`, `uploads`),
   indexes, RLS policies, and seeds three demo rivers.
3. Go to **Settings → API**, copy the **Project URL** and the
   **service_role key** (not the anon key — the backend needs write access).
4. Copy `.env.example` to `.env` and fill in:
   ```
   SUPABASE_URL=https://xxxx.supabase.co
   SUPABASE_KEY=your-service-role-key
   ```
5. Restart the app. It now reads/writes real Postgres instead of the
   in-memory fallback — you'll see the warning log disappear.

**Note on RLS:** the migration ships with permissive "allow all" policies so
the app works immediately with the service_role key. Before any real
deployment, tighten these (e.g. restrict writes to a service role only,
add auth for the dashboard itself) — see the policy section at the bottom
of the migration file.

### 2. Google Earth Engine (real satellite data)

1. Create a Google Cloud project, enable the Earth Engine API.
2. Create a service account, generate a JSON key.
3. Register the service account for Earth Engine access at
   [signup](https://signup.earthengine.google.com/).
4. In `.env`:
   ```
   GEE_SERVICE_ACCOUNT=your-service-account@project.iam.gserviceaccount.com
   GEE_PRIVATE_KEY_PATH=/path/to/key.json
   ```
5. Search a river on the dashboard (search box above the map), pick the
   right match, and it's added to monitoring immediately.

Once GEE is connected, every scan (manual or scheduled) pulls real
Sentinel-2 imagery, computes a real NDWI change score, and downloads real
before/after true-color thumbnails — these show up directly in the alert
feed and in the evidence PDF. Without GEE configured, scans still run and
return a clearly-labeled `synthetic: true` change score so the pipeline
and dashboard remain fully demoable without credentials.

### 2b. Searching for a river and automatic monitoring

The dashboard has a search box ("Search a river, e.g. 'Tungabhadra river
Davangere'") that geocodes your query via OpenStreetMap's free Nominatim
API — no key needed, just real internet access (it's blocked in sandboxed
dev environments with restricted egress, but works normally anywhere else).
Pick the right result from the list (place names collide — there are
multiple rivers named "Krishna" across states, for instance) and it's added
to the `rivers` table and starts monitoring immediately.

Every added river gets:
- `scan_interval_days` (default 5) — how often it's automatically re-scanned
- `monitoring_enabled` (default true) — toggle it off from the "Monitored
  rivers" list on the dashboard without deleting the river
- `last_scanned_at` — updates every time a scan runs, manual or automatic

A background scheduler (`backend/scheduler.py`, using APScheduler) checks
every hour whether any monitored river is overdue for its scan and runs the
full satellite pipeline automatically if so — this is what makes it "search
once, then it keeps watching on its own." You can also hit "Scan now" on
any river from the dashboard to trigger one immediately instead of waiting.

### 3. WhatsApp alerts (Twilio)

1. Create a [Twilio](https://www.twilio.com/) account, activate the WhatsApp
   sandbox (or a production WhatsApp sender once approved).
2. In `.env`:
   ```
   TWILIO_ACCOUNT_SID=...
   TWILIO_AUTH_TOKEN=...
   TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
   ```
3. Add officers with their `whatsapp_number` (E.164 format, e.g.
   `+919876543210`) to the `officers` table in Supabase, linked to a `river_id`.
4. Any HIGH or WATCH alert automatically messages every officer mapped to
   that river.

### 4. AI reasoning engine (optional upgrade)

Without an API key, the reasoning engine uses a transparent rule-based
scorer (see `backend/detection/reasoning.py`) — deterministic, explainable,
and good enough to demo and even defend in a viva since you can walk through
exactly how every point is scored.

To upgrade to LLM-written human-style reasoning:
```
ANTHROPIC_API_KEY=sk-ant-...
```
The pipeline then calls Claude to weigh the same structured signals and
write a richer plain-English judgment. If the call ever fails (bad key, rate
limit, network), it automatically falls back to the rule-based scorer —
this never breaks the pipeline.

### 5. JCB / excavator detection (important limitation, read this)

YOLOv8's pretrained COCO weights detect `person` and `truck` correctly out
of the box — verified in testing. COCO has **no excavator/JCB class**, so
out of the box JCBs will not be detected. Two options:

- **Recommended for your final year project:** fine-tune a small YOLOv8
  model on a public excavator/construction-vehicle dataset (Roboflow
  Universe has several free ones — search "excavator detection"). Takes a
  few hours on a free Colab GPU. Drop the resulting `best.pt` into `models/`
  and set `CUSTOM_JCB_MODEL_PATH=models/best.pt` in `.env` — `detector.py`
  will then run both models and merge results automatically, no other code
  changes needed.
- **For now:** the pipeline works correctly on truck + person detection
  alone, which combined with clustering and zone-checking is already a
  meaningful signal.

## Project structure

```
riverguard/
├── backend/
│   ├── app.py                  # Flask entrypoint
│   ├── config.py                # all settings, reads from .env
│   ├── pipeline.py              # ties every stage together
│   ├── detection/
│   │   ├── detector.py          # YOLOv8 wrapper (image + video)
│   │   ├── tracker.py           # lightweight IOU multi-object tracker
│   │   ├── cluster_engine.py    # DBSCAN grouping (geo + pixel mode)
│   │   ├── zone_checker.py      # river buffer polygon / distance check
│   │   └── reasoning.py         # rule-based + optional Claude reasoning
│   ├── satellite/
│   │   └── gee_client.py        # Earth Engine NDWI change detection
│   ├── alerts/
│   │   ├── notifier.py          # Twilio WhatsApp + SMTP email
│   │   └── pdf_generator.py     # evidence PDF with SHA-256 hash
│   └── api/
│       ├── routes.py            # all REST endpoints
│       └── supabase_client.py   # Supabase wrapper + in-memory fallback
├── frontend/
│   ├── index.html               # main dashboard (map + alert feed)
│   ├── upload.html              # separate manual detection page
│   ├── css/style.css
│   └── js/{dashboard,upload}.js
├── supabase/migrations/0001_init.sql
├── requirements.txt
└── .env.example
```

## API reference

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/rivers` | GET/POST | list / add monitored rivers |
| `/api/alerts` | GET | list alerts, `?severity=HIGH` to filter |
| `/api/alerts/<id>/detections` | GET | raw detection boxes for one alert |
| `/api/alerts/<id>/evidence.pdf` | GET | download the evidence report |
| `/api/alerts/<id>/annotated.jpg` | GET | annotated detection frame |
| `/api/metrics` | GET | dashboard summary counts |
| `/api/upload` | POST | multipart image/video upload → runs full pipeline |
| `/api/satellite-scan` | POST | manually trigger a satellite comparison |

## Deploying

- **Backend:** any host that runs Python (Render, Railway, a VPS). Set all
  `.env` vars as environment variables there. Don't run with
  `FLASK_DEBUG=true` in production.
- **Frontend:** served directly by Flask (`send_from_directory`), no
  separate deploy needed — it's static files in `frontend/`.
- **Scheduling satellite scans:** for real automation, wire a cron job / a
  scheduled Cloud Function to hit `POST /api/satellite-scan` for each river
  every few days, instead of triggering it manually.

## What was reused from the original hardware-drone project

Per your earlier build (the sand-mining-detection zip with Jetson/drone
hardware), the following logic was ported and adapted here, stripped of all
hardware dependencies (Jetson, MAVSDK, Flutter telemetry app, gimbal/GPS
projection math):

- River buffer polygon check → `detection/zone_checker.py`
- DBSCAN incident clustering → `detection/cluster_engine.py`
- Evidence annotation + PDF pattern → `alerts/pdf_generator.py`

Everything else here — the tracker, the reasoning engine, the satellite
tier, the Supabase schema, both dashboard pages — is new.
