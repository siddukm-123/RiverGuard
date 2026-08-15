-- RiverGuard initial schema
-- Run this in the Supabase SQL editor, or via `supabase db push` / the CLI migration flow.
-- Safe to re-run: every statement is guarded with IF NOT EXISTS / OR REPLACE.

create extension if not exists "uuid-ossp";

-- ─────────────────────────────────────────────────────────────
-- rivers: every river/stretch RiverGuard is watching
-- ─────────────────────────────────────────────────────────────
create table if not exists rivers (
    id            uuid primary key default uuid_generate_v4(),
    name          text not null,
    state         text,
    district      text,
    center_lat    double precision not null,
    center_lng    double precision not null,
    buffer_geojson jsonb,              -- legal-zone buffer polygon around the river centerline
    buffer_radius_m integer default 1000,
    monitoring_enabled boolean default true,
    scan_interval_days integer default 5,
    last_scanned_at timestamptz,
    created_at    timestamptz not null default now()
);

-- Safe to re-run against a database created by an earlier version of this
-- migration that predates the scheduling columns:
alter table rivers add column if not exists monitoring_enabled boolean default true;
alter table rivers add column if not exists scan_interval_days integer default 5;
alter table rivers add column if not exists last_scanned_at timestamptz;
alter table rivers add column if not exists monitor_points jsonb;

create index if not exists idx_rivers_name on rivers (lower(name));

-- ─────────────────────────────────────────────────────────────
-- officers: who gets notified for a given river/district
-- ─────────────────────────────────────────────────────────────
create table if not exists officers (
    id              uuid primary key default uuid_generate_v4(),
    river_id        uuid references rivers(id) on delete set null,
    name            text not null,
    designation     text,
    phone           text,
    whatsapp_number text,
    email           text,
    district        text,
    created_at      timestamptz not null default now()
);

create index if not exists idx_officers_river on officers (river_id);

-- ─────────────────────────────────────────────────────────────
-- alerts: one row per confirmed/watched detection event
-- source: 'satellite' (scheduled GEE scan) or 'manual_upload' (image/video uploaded on the detection page)
-- severity: HIGH / WATCH / SAFE
-- ─────────────────────────────────────────────────────────────
create table if not exists alerts (
    id              uuid primary key default uuid_generate_v4(),
    river_id        uuid references rivers(id) on delete set null,
    source          text not null default 'satellite' check (source in ('satellite', 'manual_upload')),
    severity        text not null check (severity in ('HIGH', 'WATCH', 'SAFE')),
    confidence      numeric(5,2) not null default 0,       -- 0-100
    area_sqm        numeric,
    lat             double precision,
    lng             double precision,
    inside_buffer   boolean default false,
    reasoning_text  text,                                   -- the AI reasoning-engine explanation
    reasoning_signals jsonb,                                 -- structured signals fed to the reasoner (dwell time, convoy pattern, etc)
    before_image_url text,
    after_image_url  text,
    annotated_image_path text,
    evidence_pdf_url text,
    evidence_sha256   text,
    whatsapp_status text default 'not_sent' check (whatsapp_status in ('not_sent', 'sent', 'failed')),
    email_status    text default 'not_sent' check (email_status in ('not_sent', 'sent', 'failed')),
    detected_at     timestamptz not null default now(),
    created_at      timestamptz not null default now()
);

create index if not exists idx_alerts_river on alerts (river_id);
create index if not exists idx_alerts_severity on alerts (severity);
create index if not exists idx_alerts_detected_at on alerts (detected_at desc);

-- ─────────────────────────────────────────────────────────────
-- detections: individual object-detection boxes belonging to an alert
-- (truck / jcb / person), with optional track_id for video uploads
-- ─────────────────────────────────────────────────────────────
create table if not exists detections (
    id           uuid primary key default uuid_generate_v4(),
    alert_id     uuid references alerts(id) on delete cascade,
    class_name   text not null,             -- 'truck' | 'jcb' | 'person'
    confidence   numeric(5,2) not null,
    bbox_x_min   integer,
    bbox_y_min   integer,
    bbox_x_max   integer,
    bbox_y_max   integer,
    frame_number integer,                   -- null for still images
    track_id     integer,                   -- stable id across frames for a video upload
    lat          double precision,
    lng          double precision,
    created_at   timestamptz not null default now()
);

create index if not exists idx_detections_alert on detections (alert_id);
create index if not exists idx_detections_track on detections (alert_id, track_id);

-- ─────────────────────────────────────────────────────────────
-- evidence: files (images/PDF) tied to an alert, hashed for tamper-evidence
-- ─────────────────────────────────────────────────────────────
create table if not exists evidence (
    id           uuid primary key default uuid_generate_v4(),
    alert_id     uuid references alerts(id) on delete cascade,
    file_type    text not null check (file_type in ('image', 'pdf', 'video_clip')),
    file_path    text not null,            -- storage path or public URL
    sha256_hash  text not null,
    created_at   timestamptz not null default now()
);

create index if not exists idx_evidence_alert on evidence (alert_id);

-- ─────────────────────────────────────────────────────────────
-- uploads: tracks every manual image/video submitted on the detection page
-- ─────────────────────────────────────────────────────────────
create table if not exists uploads (
    id             uuid primary key default uuid_generate_v4(),
    original_name  text,
    file_type      text check (file_type in ('image', 'video')),
    storage_path   text,
    status         text default 'processing' check (status in ('processing', 'done', 'failed')),
    result_alert_id uuid references alerts(id) on delete set null,
    error_message  text,
    uploaded_at    timestamptz not null default now()
);

create index if not exists idx_uploads_status on uploads (status);

-- ─────────────────────────────────────────────────────────────
-- Row Level Security
-- Demo-friendly policies: public read on everything, public insert on
-- alerts/detections/evidence/uploads (so the Flask backend using the anon
-- key can write). Tighten these before any real deployment — see README.
-- ─────────────────────────────────────────────────────────────
alter table rivers      enable row level security;
alter table officers    enable row level security;
alter table alerts      enable row level security;
alter table detections  enable row level security;
alter table evidence    enable row level security;
alter table uploads     enable row level security;

drop policy if exists "public read rivers" on rivers;
create policy "public read rivers" on rivers for select using (true);
drop policy if exists "public write rivers" on rivers;
create policy "public write rivers" on rivers for insert with check (true);
drop policy if exists "public update rivers" on rivers;
create policy "public update rivers" on rivers for update using (true);
drop policy if exists "public delete rivers" on rivers;
create policy "public delete rivers" on rivers for delete using (true);

drop policy if exists "public read officers" on officers;
create policy "public read officers" on officers for select using (true);

drop policy if exists "public read alerts" on alerts;
create policy "public read alerts" on alerts for select using (true);
drop policy if exists "public write alerts" on alerts;
create policy "public write alerts" on alerts for insert with check (true);
drop policy if exists "public update alerts" on alerts;
create policy "public update alerts" on alerts for update using (true);

drop policy if exists "public read detections" on detections;
create policy "public read detections" on detections for select using (true);
drop policy if exists "public write detections" on detections;
create policy "public write detections" on detections for insert with check (true);

drop policy if exists "public read evidence" on evidence;
create policy "public read evidence" on evidence for select using (true);
drop policy if exists "public write evidence" on evidence;
create policy "public write evidence" on evidence for insert with check (true);

drop policy if exists "public read uploads" on uploads;
create policy "public read uploads" on uploads for select using (true);
drop policy if exists "public write uploads" on uploads;
create policy "public write uploads" on uploads for insert with check (true);
drop policy if exists "public update uploads" on uploads;
create policy "public update uploads" on uploads for update using (true);

-- ─────────────────────────────────────────────────────────────
-- Seed data: a few sample rivers + officers so the dashboard isn't empty
-- on first run. Safe to delete once you add real data.
-- ─────────────────────────────────────────────────────────────
insert into rivers (name, state, district, center_lat, center_lng)
select 'Cauvery', 'Tamil Nadu', 'Karur', 10.9601, 78.0766
where not exists (select 1 from rivers where name = 'Cauvery');

insert into rivers (name, state, district, center_lat, center_lng)
select 'Brahmaputra', 'Assam', 'Guwahati', 26.1445, 91.7362
where not exists (select 1 from rivers where name = 'Brahmaputra');

insert into rivers (name, state, district, center_lat, center_lng)
select 'Tungabhadra', 'Karnataka', 'Davangere', 14.4644, 75.9218
where not exists (select 1 from rivers where name = 'Tungabhadra');
