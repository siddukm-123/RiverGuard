const map = L.map('map', { zoomControl: true }).setView([21.0, 79.0], 5);

// Satellite-style basemap (Esri World Imagery)
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics',
  maxZoom: 19,
}).addTo(map);

// Labels overlay so place names remain readable on satellite imagery.
// Esri's free reference layer (same provider as the imagery, no API key). The old CARTO overlay now requires a key
// and painted an "API KEY REQUIRED" watermark over the whole map.
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Labels &copy; Esri',
  maxZoom: 19,
  maxNativeZoom: 13,  // Esri labels exist up to this level; Leaflet scales them beyond it instead of showing blank tiles
  opacity: 0.9,
}).addTo(map);

const severityColor = { HIGH: '#E4572E', WATCH: '#D9A441', SAFE: '#3FA796' };
const riverMarkers = {};
let alertMarkersLayer = L.layerGroup().addTo(map);
let alertZonesLayer = L.layerGroup().addTo(map);
let corridorLayer = L.layerGroup().addTo(map);

function parseSignals(alert) {
  let s = alert.reasoning_signals;
  if (typeof s === 'string') {
    try { s = JSON.parse(s); } catch { s = {}; }
  }
  return s || {};
}

function boundsAround(lat, lng, radiusM = 700) {
  const dLat = radiusM / 111320;
  const dLng = radiusM / (111320 * Math.cos(lat * Math.PI / 180));
  return [[lat - dLat, lng - dLng], [lat + dLat, lng + dLng]];
}

function drawAlertZone(lat, lng, style, popup) {
  L.rectangle(boundsAround(lat, lng, style.radiusM || 700), {
    color: style.color,
    weight: style.weight || 3,
    fillColor: style.color,
    fillOpacity: style.fillOpacity ?? 0.05,
    dashArray: style.dashArray || null,
  }).bindPopup(popup).addTo(alertZonesLayer);
}

function timeAgo(iso) {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

let riverLayerGroup = L.layerGroup().addTo(map);

async function loadRivers() {
  const res = await apiFetch('/api/rivers');
  const rivers = await res.json();
  riverLayerGroup.clearLayers();
  corridorLayer.clearLayers();
  rivers.forEach(r => {
    L.circleMarker([r.center_lat, r.center_lng], {
      radius: 7, color: '#4FD1C5', fillColor: '#4FD1C5', fillOpacity: 0.6, weight: 2,
    }).addTo(riverLayerGroup).bindTooltip(r.name, { permanent: false });

    if (r.monitor_points && r.monitor_points.length) {
      const latlngs = r.monitor_points.map(pt => [pt.lat, pt.lng]);
      L.polyline(latlngs, { color: '#4FD1C5', weight: 3, opacity: 0.85, dashArray: null })
        .addTo(corridorLayer).bindTooltip(`${r.name} — scan corridor along river flow`);
      r.monitor_points.forEach((pt, i) => {
        L.circleMarker([pt.lat, pt.lng], {
          radius: 5, color: '#D9A441', fillColor: '#D9A441', fillOpacity: 0.7, weight: 2,
        }).addTo(corridorLayer).bindTooltip(`${r.name} — ${pt.label || 'bed point ' + (i + 1)}`);
      });
    }
    riverMarkers[r.id] = r;
  });
  renderRiversList(rivers);
}

function renderRiversList(rivers) {
  const el = document.getElementById('rivers-list');
  if (!el) return;
  if (rivers.length === 0) {
    el.innerHTML = '<div class="empty-state">No rivers added yet — search above.</div>';
    return;
  }
  el.innerHTML = rivers.map(r => {
    const lastScan = r.last_scanned_at ? timeAgo(r.last_scanned_at) : 'never';
    const enabled = r.monitoring_enabled !== false;
    const points = r.monitor_points ? r.monitor_points.length : 0;
    return `
      <div class="alert-item" style="display:flex; align-items:center; justify-content:space-between; cursor:default;">
        <div>
          <strong>${escapeHtml(r.name)}</strong>
          <div class="meta">${escapeHtml(r.district || r.state || '')}${points ? ` · ${points} corridor points` : ''} · scans every ${r.scan_interval_days || 5}d · last scan: ${lastScan}</div>
        </div>
        <div style="display:flex; gap:8px; align-items:center; flex-shrink:0;">
          <button class="btn" onclick="focusRiver('${r.id}')" style="padding:6px 10px; font-size:12px;">Show</button>
          <a href="/details" class="btn" style="padding:6px 10px; font-size:12px; text-decoration:none;" onclick="sessionStorage.setItem('openRiver','${r.id}')">Details</a>
          <button class="btn" onclick="triggerScanNow('${r.id}', this)" style="padding:6px 10px; font-size:12px;">Scan now</button>
          <button class="btn ${enabled ? 'primary' : ''}" onclick="toggleMonitoring('${r.id}', ${!enabled})" style="padding:6px 10px; font-size:12px;">${enabled ? 'Monitoring ON' : 'Monitoring OFF'}</button>
          <button class="btn danger" onclick="deleteRiver('${r.id}')" style="padding:6px 10px; font-size:12px;">Delete</button>
        </div>
      </div>
    `;
  }).join('');
}

function focusRiver(riverId) {
  const r = riverMarkers[riverId];
  if (!r) return;
  map.flyTo([r.center_lat, r.center_lng], 11, { duration: 0.8 });
}

async function toggleMonitoring(riverId, newState) {
  await apiFetch(`/api/rivers/${riverId}/monitoring`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ monitoring_enabled: newState }),
  });
  loadRivers();
}

async function deleteRiver(riverId) {
  const r = riverMarkers[riverId];
  const name = r ? r.name : 'this river';
  if (!confirm(`Delete "${name}" from monitoring?\n\nThis removes the river and all its scan alerts.`)) return;
  const res = await apiFetch(`/api/rivers/${riverId}`, { method: 'DELETE' });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    alert(data.error || 'Failed to delete river.');
    return;
  }
  await loadRivers();
  await refreshAll();
}

async function triggerScanNow(riverId, btn) {
  const button = btn || event.target;
  button.textContent = 'Scanning…';
  button.disabled = true;
  try {
    const res = await apiFetch('/api/satellite-scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ river_id: riverId, async: true }),
    });
    const first = await res.json().catch(() => ({}));
    if (!res.ok && res.status !== 202) {
      alert(first.error || 'Scan failed — check server logs.');
      return;
    }
    const result = res.status === 202
      ? await pollJob(first.job_id, (job, secs) => { button.textContent = `Scanning… ${secs}s`; })
      : first;
    if (result && result.status === 'no_data') {
      // Honest outcome: no usable before/after pair, so nothing was invented and no alert was raised.
      alert('No alert was created.\n\n' + (result.message || 'No usable Sentinel-2 comparison was available.'));
    }
    await refreshAll();
    await loadRivers();
  } catch (e) {
    alert(e.message || 'Scan failed.');
  } finally {
    button.disabled = false;
    button.textContent = 'Scan now';
  }
}

// ── River search / add ──────────────────────────────────────────────
const searchInput = document.getElementById('river-search-input');
const searchBtn = document.getElementById('river-search-btn');
const searchResults = document.getElementById('river-search-results');
let searchCandidates = [];

async function doRiverSearch() {
  const q = searchInput.value.trim();
  if (!q) return;
  searchResults.innerHTML = '<div class="empty-state">Searching…</div>';
  try {
    const res = await apiFetch(`/api/rivers/search?q=${encodeURIComponent(q)}`);
    const results = await res.json();
    if (!res.ok || results.error) {
      searchResults.innerHTML = `<div class="empty-state">${escapeHtml(results.error || 'Search failed.')}</div>`;
      return;
    }
    if (results.length === 0) {
      searchResults.innerHTML = '<div class="empty-state">No matches found. Try a more specific name (e.g. "Tungabhadra river Davangere").</div>';
      return;
    }
    searchCandidates = results;
    searchResults.innerHTML = results.map((r, i) => `
      <div class="alert-item search-result-item" data-index="${i}" style="cursor:pointer;">
        <strong>${escapeHtml(r.name)}</strong>
        <div class="meta">${escapeHtml(r.display_name)}</div>
        <div class="meta" style="margin-top:4px; color: var(--accent-teal);">Click to add →</div>
      </div>
    `).join('');
    searchResults.querySelectorAll('.search-result-item').forEach(el => {
      el.addEventListener('click', () => addRiverFromSearch(searchCandidates[Number(el.dataset.index)]));
    });
  } catch (e) {
    searchResults.innerHTML = '<div class="empty-state">Search failed — check your internet connection.</div>';
  }
}

async function addRiverFromSearch(r) {
  searchResults.innerHTML = '<div class="empty-state">Adding river and mapping corridor points…</div>';
  try {
    const res = await apiFetch('/api/rivers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: r.name, state: r.state, district: r.district,
        center_lat: r.lat, center_lng: r.lng,
      }),
    });
    let data = {};
    try { data = await res.json(); } catch { data = {}; }
    if (!res.ok) {
      searchResults.innerHTML = `<div class="empty-state" style="color: var(--accent-high);">${escapeHtml(data.error || `Failed to add river (${res.status}).`)}</div>`;
      return;
    }
    searchResults.innerHTML = `<div class="empty-state" style="color: var(--accent-teal);">Added <strong>${escapeHtml(data.name)}</strong> on river channel with ${(data.monitor_points || []).length} monitor points.</div>`;
    searchInput.value = '';
    await loadRivers();
    map.flyTo([data.center_lat, data.center_lng], 12, { duration: 0.8 });
    document.getElementById('rivers-list')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    setTimeout(() => { searchResults.innerHTML = ''; }, 5000);
  } catch (e) {
    searchResults.innerHTML = '<div class="empty-state" style="color: var(--accent-high);">Failed to add river — check server terminal for errors.</div>';
  }
}

searchBtn.addEventListener('click', doRiverSearch);
searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') doRiverSearch(); });

async function loadMetrics() {
  const res = await apiFetch('/api/metrics');
  const m = await res.json();
  document.getElementById('m-rivers').textContent = m.rivers_monitored;
  document.getElementById('m-high').textContent = m.high_severity;
  document.getElementById('m-watch').textContent = m.watch_severity;
  let scanned = m.rivers_scanned;
  if (scanned === undefined) {   // backend not updated yet: count from the river list (same "last scan" the list shows)
    try {
      const rr = await apiFetch('/api/rivers');
      scanned = (await rr.json()).filter(r => r.last_scanned_at).length;
    } catch { scanned = '—'; }
  }
  document.getElementById('m-scanned').textContent = scanned;
}

async function loadAlerts() {
  const res = await apiFetch('/api/alerts');
  const alerts = await res.json();
  const feed = document.getElementById('alert-feed');
  alertMarkersLayer.clearLayers();
  alertZonesLayer.clearLayers();

  if (alerts.length === 0) {
    feed.innerHTML = '<div class="empty-state">No alerts yet. Trigger a satellite scan or run manual detection to populate this feed.</div>';
    return;
  }

  feed.innerHTML = alerts.map(a => {
    const signals = parseSignals(a);
    const isReal = a.source === 'satellite' && signals.synthetic_data === false;
    const isDemo = a.source === 'satellite' && signals.synthetic_data === true;
    const dataLabel = isReal ? `✓ real Sentinel-2: ${escapeHtml(signals.before_date || '?')} → ${escapeHtml(signals.after_date || '?')}` : (isDemo ? '⚠ SYNTHETIC demo data — not a real observation (GEE not connected)' : '');
    const corridorInfo = signals.corridor_points_scanned ? ` · ${signals.corridor_points_scanned} corridor points scanned` : '';
    return `
    <div class="alert-item" onclick="focusAlert(${a.lat}, ${a.lng})">
      <span class="severity-badge ${a.severity}">${a.severity}</span>
      <span style="font-family: var(--font-mono); font-size: 11.5px; color: var(--text-muted); margin-left: 8px;">risk ${(a.confidence || 0).toFixed(0)}/100</span>
      <div class="meta">${a.source === 'satellite' ? '🛰 satellite' : '📹 manual upload'} · ${timeAgo(a.detected_at)}${corridorInfo}</div>
      ${dataLabel ? `<div class="meta" style="color:${isReal ? 'var(--accent-teal)' : 'var(--accent-watch)'};">${dataLabel}</div>` : ''}
      ${signals.hotspot_label ? `<div class="meta">Hotspot: ${escapeHtml(signals.hotspot_label)} · corridor-relative change ${((signals.satellite_change_score || 0) * 100).toFixed(0)}%${signals.rain_confounder ? ' · ⚠ heavy rain between scenes' : ''}</div>` : ''}
      ${a.source === 'manual_upload' && signals.object_counts ? `<div class="meta">${Object.entries(signals.object_counts).map(([k, v]) => `${escapeHtml(k)}: ${v}`).join(' · ') || 'no relevant objects'}${signals.lowlight && (signals.lowlight.applied || signals.lowlight.frames_enhanced) ? ' · low-light enhanced' : ''}</div>` : ''}
      <div class="reasoning">${escapeHtml(a.reasoning_text || '')}</div>
      ${a.before_image_url && a.after_image_url ? `
        <div style="display:flex; gap:6px; margin-top:8px;">
          <div style="flex:1;"><img src="/api/alerts/${a.id}/before.jpg" style="width:100%; border-radius:6px; border:1px solid var(--border);"><div class="meta" style="text-align:center; margin-top:2px;">before</div></div>
          <div style="flex:1;"><img src="/api/alerts/${a.id}/after.jpg" style="width:100%; border-radius:6px; border:1px solid var(--border);"><div class="meta" style="text-align:center; margin-top:2px;">after</div></div>
        </div>
      ` : (a.source === 'satellite' ? '<div class="meta" style="margin-top:6px;">No before/after thumbnails available for this scan.</div>' : '')}
      ${a.evidence_pdf_url ? `<a href="/api/alerts/${a.id}/evidence.pdf" target="_blank" style="font-size:11.5px; color: var(--accent-teal); text-decoration:none;">↓ evidence PDF</a>` : ''}
    </div>`;
  }).join('');

  alerts.forEach(a => {
    if (a.lat == null || a.lng == null) return;
    const signals = parseSignals(a);
    const color = severityColor[a.severity] || '#7E9296';
    const popup = `<strong>${a.severity}</strong> (risk ${(a.confidence || 0).toFixed(0)}/100)<br>${escapeHtml(a.reasoning_text || '')}`;

    // Red / yellow bordered zone boxes on the map (not filled solid)
    if (a.severity === 'HIGH') {
      drawAlertZone(a.lat, a.lng, { color: '#E4572E', weight: 3, fillOpacity: 0.07, radiusM: 750 }, popup);
    } else if (a.severity === 'WATCH') {
      drawAlertZone(a.lat, a.lng, { color: '#D9A441', weight: 2.5, fillOpacity: 0.06, dashArray: '10 6', radiusM: 650 }, popup);
    } else if ((signals.satellite_change_score || 0) > 0.2) {
      drawAlertZone(a.lat, a.lng, { color: '#D9A441', weight: 2, fillOpacity: 0.04, dashArray: '6 8', radiusM: 550 }, popup);
    }

    L.circleMarker([a.lat, a.lng], {
      radius: a.severity === 'HIGH' ? 8 : 6,
      color, fillColor: color, fillOpacity: 0.85, weight: 2,
    }).bindPopup(popup).addTo(alertMarkersLayer);

    // Yellow boxes for other corridor points with moderate change (low confidence hotspots)
    const scanResults = signals.scan_results || [];
    scanResults.forEach(sr => {
      if (sr.lat == null || sr.lng == null) return;
      const isMain = Math.abs(sr.lat - a.lat) < 0.0002 && Math.abs(sr.lng - a.lng) < 0.0002;
      const score = sr.change_score || 0;
      if (isMain || score < 0.2) return;
      drawAlertZone(sr.lat, sr.lng, {
        color: '#D9A441',
        weight: 2,
        fillOpacity: 0.03,
        dashArray: '5 7',
        radiusM: 500,
      }, `<strong>Corridor watch</strong><br>${escapeHtml(sr.label || 'segment')}<br>Corridor-relative change: ${(score * 100).toFixed(0)}%`);
    });
  });
}

function focusAlert(lat, lng) {
  if (lat == null || lng == null) return;
  map.flyTo([lat, lng], 13, { duration: 0.8 });
}

async function refreshAll() {
  await Promise.all([loadMetrics(), loadAlerts()]);
}

async function loadSystemStatus() {
  const el = document.getElementById('status-banner');
  if (!el) return;
  try {
    const res = await apiFetch('/api/system-status');
    const st = await res.json();
    const notes = [];
    if (!st.storage_persistent) notes.push('⚠ In-memory storage: data is lost on restart (Supabase not connected).');
    if (st.satellite !== 'real_gee') notes.push('⚠ Satellite results are SYNTHETIC demo values (Google Earth Engine not configured).');
    if (st.model && !st.model.excavator_detection_available) notes.push('ℹ Excavator/JCB detection is not available with the loaded model (trucks and people only).');
    if (!st.write_protection) notes.push('ℹ Write protection is off (RIVERGUARD_API_KEY not set).');
    el.style.display = notes.length ? 'block' : 'none';
    el.innerHTML = notes.map(escapeHtml).join('<br>');
  } catch { el.style.display = 'none'; }
}

loadSystemStatus();
loadRivers();
refreshAll();
setInterval(refreshAll, 30000);
