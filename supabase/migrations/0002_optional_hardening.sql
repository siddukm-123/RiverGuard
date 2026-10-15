-- OPTIONAL. The app works without running this file (extra data lives in alerts.reasoning_signals jsonb).

-- 1) Per-detection provenance columns (the app auto-detects them and falls back if absent)
alter table detections add column if not exists timestamp_s double precision;
alter table detections add column if not exists view text;       -- original | enhanced | both
alter table detections add column if not exists model text;      -- base | custom
alter table detections add column if not exists role text;       -- evidence | context
alter table detections add column if not exists raw_class text;

-- 2) Tighten Row Level Security.  *** Run ONLY if the Railway SUPABASE_KEY is the service_role key ***
--    (check: GET /api/system-status -> "supabase_key_role": "service_role"). The service role bypasses RLS,
--    so dropping the public policies blocks anyone holding only the public anon key from reading/writing.
-- drop policy if exists "public read rivers" on rivers;      drop policy if exists "public write rivers" on rivers;
-- drop policy if exists "public update rivers" on rivers;    drop policy if exists "public delete rivers" on rivers;
-- drop policy if exists "public read officers" on officers;
-- drop policy if exists "public read alerts" on alerts;      drop policy if exists "public write alerts" on alerts;
-- drop policy if exists "public update alerts" on alerts;
-- drop policy if exists "public read detections" on detections; drop policy if exists "public write detections" on detections;
-- drop policy if exists "public read evidence" on evidence;  drop policy if exists "public write evidence" on evidence;
-- drop policy if exists "public read uploads" on uploads;    drop policy if exists "public write uploads" on uploads;
-- drop policy if exists "public update uploads" on uploads;
