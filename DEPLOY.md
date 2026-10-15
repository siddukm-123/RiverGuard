# RiverGuard — Deployment Guide

Deploy the **frontend on Vercel** and the **Python/Flask backend on Railway**.  
Secrets never go in Git — only in each platform's environment-variable settings.

---

## Architecture

```
User browser
    │
    ▼
Vercel (static frontend: HTML/JS/CSS)
    │  /api/* rewritten to ──►  Railway (Flask + Gunicorn)
    │                              │
    │                              ├── Supabase (database)
    │                              ├── Google Earth Engine (satellite)
    │                              └── Disk (uploads/evidence)
```

---

## Environment variables — quick reference

### Vercel (frontend) — Settings → Environment Variables

The frontend is **static HTML/JS**. It does **not** need Supabase keys, GEE keys, or any backend secrets.

| Variable | Required? | Example value | Purpose |
|----------|-----------|---------------|---------|
| `RAILWAY_BACKEND_URL` | **Yes** (recommended) | `https://riverguard-production.up.railway.app` | Build script patches `vercel.json` so `/api/*` proxies to Railway. **No trailing slash.** |
| `VITE_API_BASE` | No | *(leave empty)* | Only if you skip Vercel rewrites and call Railway directly from the browser. Usually leave **empty**. |

**How to add on Vercel:**
1. Project → **Settings** → **Environment Variables**
2. Add `RAILWAY_BACKEND_URL` = your Railway public URL
3. Enable for **Production**, **Preview**, and **Development**
4. Redeploy

**Do NOT add these on Vercel:**

| Never on Vercel | Why |
|-----------------|-----|
| `SUPABASE_KEY` | Backend secret — would be exposed in browser |
| `GEE_PRIVATE_KEY_JSON` | Google credentials |
| `ANTHROPIC_API_KEY` | Paid API key |
| `TWILIO_AUTH_TOKEN` | Secret |
| `FLASK_SECRET_KEY` | Backend only |
| Any `.env` file contents | Use platform UI instead |

---

### Railway (backend) — Service → Variables

All real secrets and API keys go **here only**.

#### Required (minimum working deploy)

| Variable | Example value | Notes |
|----------|---------------|-------|
| `SUPABASE_URL` | `https://abcdefgh.supabase.co` | Supabase → Settings → API → Project URL |
| `SUPABASE_KEY` | `eyJhbGciOiJIUzI1NiIs...` | **service_role** key (NOT anon/public) |
| `FLASK_SECRET_KEY` | `a1b2c3d4e5f6...` (32+ random chars) | Session security — generate a random string |
| `FLASK_DEBUG` | `false` | Must be `false` in production |

#### Required for real satellite scans (GEE)

| Variable | Example value | Notes |
|----------|---------------|-------|
| `GEE_SERVICE_ACCOUNT` | `riverguard@project.iam.gserviceaccount.com` | Google service account email |
| `GEE_PRIVATE_KEY_JSON` | `{"type":"service_account",...}` | Paste **entire** JSON from `secrets/gee-key.json` |
| `GEE_PROJECT_ID` | `riverguard-505210` | Optional — auto-detected from service account email |

**GEE on Railway:** Open your local `secrets/gee-key.json`, copy all JSON, paste as one variable. Do not upload the file.

#### Recommended for production

| Variable | Example value | Notes |
|----------|---------------|-------|
| `CORS_ORIGIN` | `https://your-app.vercel.app` | Your Vercel domain (only needed if not using Vercel rewrites) |
| `ENABLE_SCHEDULER` | `true` | Auto satellite scans every few days |
| `UPLOAD_DIR` | `/app/data/uploads` | Use with Railway **Volume** |
| `EVIDENCE_DIR` | `/app/data/evidence` | Use with Railway **Volume** |

#### Optional features

| Variable | Example value | Purpose |
|----------|---------------|---------|
| `ANTHROPIC_API_KEY` | `sk-ant-...` | AI reasoning (Claude) |
| `REASONING_MODEL` | `claude-sonnet-4-6` | Claude model name |
| `TWILIO_ACCOUNT_SID` | `ACxxxxxxxx` | WhatsApp alerts |
| `TWILIO_AUTH_TOKEN` | `your-twilio-token` | WhatsApp alerts |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` | Twilio sandbox/production number |
| `SMTP_HOST` | `smtp.gmail.com` | Email alerts |
| `SMTP_PORT` | `587` | Email port |
| `SMTP_USER` | `you@gmail.com` | Email sender |
| `SMTP_PASSWORD` | `app-password` | Gmail app password |
| `YOLO_MODEL_PATH` | `yolov8n.pt` | Default — auto-downloads |
| `DETECTION_CONF_THRESHOLD` | `0.35` | Detection sensitivity |
| `DEFAULT_BUFFER_RADIUS_M` | `1000` | River buffer zone (meters) |
| `MAX_UPLOAD_MB` | `200` | Max upload size |

#### Auto-set by Railway (do not override unless needed)

| Variable | Notes |
|----------|-------|
| `PORT` | Railway sets automatically — Gunicorn binds to this |

---

## Copy-paste templates

### Vercel — minimum

```
RAILWAY_BACKEND_URL=https://YOUR-APP.up.railway.app
```

### Railway — minimum

```
SUPABASE_URL=https://YOUR-PROJECT.supabase.co
SUPABASE_KEY=YOUR-SERVICE-ROLE-KEY
FLASK_SECRET_KEY=generate-a-long-random-string-here
FLASK_DEBUG=false
ENABLE_SCHEDULER=true
```

### Railway — with Google Earth Engine

Add to the minimum set above:

```
GEE_SERVICE_ACCOUNT=your-sa@your-project.iam.gserviceaccount.com
GEE_PRIVATE_KEY_JSON={"type":"service_account","project_id":"..."}
CORS_ORIGIN=https://your-app.vercel.app
```

### Railway — with persistent uploads (Volume mounted at `/app/data`)

```
UPLOAD_DIR=/app/data/uploads
EVIDENCE_DIR=/app/data/evidence
```

---

## Before you deploy — security checklist

- [ ] `.env` is **not** committed
- [ ] `secrets/gee-key.json` is **not** committed
- [ ] Supabase **service_role** key is only on **Railway**
- [ ] No backend secrets on **Vercel**
- [ ] `FLASK_DEBUG=false` on Railway
- [ ] `FLASK_SECRET_KEY` is a strong random value

---

## Step 1 — Supabase (database)

1. Create a project at [supabase.com](https://supabase.com)
2. **SQL Editor** → run `supabase/migrations/0001_init.sql`
3. **Settings → API** → copy Project URL and **service_role** key for Railway

---

## Step 2 — Deploy backend on Railway

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Add all **Railway variables** from the tables above
3. **Settings → Networking → Generate Domain** → copy URL for Vercel
4. (Optional) Add **Volume** at `/app/data` + set `UPLOAD_DIR` / `EVIDENCE_DIR`

Health check: `https://YOUR-RAILWAY-URL/api/metrics`

---

## Step 3 — Deploy frontend on Vercel

1. [vercel.com](https://vercel.com) → **Add New Project** → import repo
2. **Framework Preset:** Other
3. **Root Directory:** `.` (repo root)
4. **Build Command:** `npm run build` (from `vercel.json`)
5. **Output Directory:** `frontend`
6. Add **Vercel variable:** `RAILWAY_BACKEND_URL` = your Railway URL
7. Deploy

The build script (`scripts/prepare-vercel.js`) automatically wires `/api` to Railway — you do **not** need to edit `vercel.json` manually if `RAILWAY_BACKEND_URL` is set.

**Manual fallback:** Edit `vercel.json` and replace `REPLACE-WITH-YOUR-RAILWAY-URL` if you skip the env var.

---

## Option B — Railway only (all-in-one)

Deploy only to Railway — Flask serves frontend + API. Use the same **Railway variables** above. No Vercel needed.

---

## Post-deploy verification

1. Open Vercel URL → Dashboard loads
2. Metrics show river count (proves API proxy works)
3. Add a river → **Scan now** → alert with before/after images
4. **Details** tab shows scan history

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| API 502 on Vercel | Check `RAILWAY_BACKEND_URL` on Vercel; redeploy after setting it |
| CORS error | Set `CORS_ORIGIN` on Railway to your Vercel URL |
| GEE init failed | Add Service Usage Consumer role in Google Cloud IAM |
| Supabase 400/500 | Re-run migration SQL; verify service_role key on Railway |
| Scan timeout | Normal (~2–3 min for multi-point GEE) |
| Uploads lost on redeploy | Add Railway volume + `UPLOAD_DIR` / `EVIDENCE_DIR` |

---

## Local development

```bash
pip install -r requirements.txt
copy .env.example .env    # fill in values locally
cd backend
python app.py
```

Open `http://localhost:8000` — no Vercel env vars needed locally.

---

## Deploy files in this repo

| File | Purpose |
|------|---------|
| `DEPLOY.md` | This guide |
| `Procfile` / `railway.toml` | Railway + Gunicorn |
| `vercel.json` | Vercel hosting + API rewrite template |
| `package.json` + `scripts/prepare-vercel.js` | Vercel build — injects `RAILWAY_BACKEND_URL` |
| `frontend/js/api.js` | API fetch helper |
| `frontend/js/env-config.js` | Local dev fallback / Vercel build output |
| `.gitignore` | Blocks secrets and runtime data |


---

## v2 update — what to change on Railway / Vercel / Supabase

**Nothing is required.** The app boots with your existing variables. Everything below is optional.

| Where | Action | Why |
|---|---|---|
| Railway | Redeploy (push the new files). Dockerfile now bakes YOLO weights and `railway.toml` has `healthcheckPath=/api/health` | faster first detection |
| Railway (recommended) | Add `RIVERGUARD_API_KEY=<long random>` | write protection; the UI asks for it once per tab |
| Railway (recommended) | Mount a Volume at `/app/data` + `UPLOAD_DIR`/`EVIDENCE_DIR` (already in this guide) | evidence/images survive redeploys |
| Railway (optional) | `SAT_BASELINE_DAYS`, `SAT_MIN_SEPARATION_DAYS`, `SAT_MIN_VALID_FRACTION`, `NOTIFY_COOLDOWN_HOURS`, `HIGH_REQUIRES_CORROBORATION`, `FOLD_VEHICLES_AS_TRUCK`, `VIDEO_MAX_SECONDS`, `CUSTOM_JCB_MODEL_PATH` | defaults are safe; see `.env.example` |
| Vercel | nothing | `.vercelignore` is now correctly named |
| Supabase | optional: run `supabase/migrations/0002_optional_hardening.sql` (provenance columns). Its RLS-tightening block is commented out; enable only after `GET /api/system-status` shows `"supabase_key_role":"service_role"` | hardening |

After deploy open `/api/system-status`: it tells you whether storage is persistent, GEE is real or synthetic, whether the model has excavator support, and whether notifications are configured.

Behaviour changes you will notice: satellite scans compare scenes ~30 days apart and may answer *"No usable Sentinel-2 comparison"* instead of inventing a number; satellite-only alerts can no longer be HIGH; uploads without GPS no longer raise WATCH by themselves; scans/videos run as background jobs.

Known limits: jobs live in the single gunicorn worker (keep `--workers 1`); very large videos through the Vercel rewrite may hit proxy body limits — if so, set Vercel `VITE_API_BASE` to the Railway URL (CORS is open by default) so uploads go direct.
