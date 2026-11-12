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

function errorText(d, status, fallback) {
  return [(d && d.error) || fallback || `Request failed (${status})`, d && d.detail && d.detail !== d.error ? `— ${d.detail}` : '', d && d.ref ? `(ref ${d.ref})` : ''].filter(Boolean).join(' ');
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

    if (r.radius_km) {
      L.circle([r.center_lat, r.center_lng], { radius: r.radius_km * 1000, color: '#7C9CFF', weight: 1.5, dashArray: '6 6', fillOpacity: 0.04 })
        .addTo(riverLayerGroup).bindTooltip(`${r.name} — watch radius ${r.radius_km} km${r.place_name ? ' · ' + r.place_name : ''}`);
    }
    if (r.monitor_points && r.monitor_points.length) {
      const byRiver = {};                                   // a live area can hold several rivers: one line per river, never joined
      r.monitor_points.forEach(pt => (byRiver[pt.river || ''] = byRiver[pt.river || ''] || []).push(pt));
      Object.entries(byRiver).forEach(([rv, g]) => L.polyline(g.map(pt => [pt.lat, pt.lng]), { color: '#4FD1C5', weight: 3, opacity: 0.85, dashArray: null })
        .addTo(corridorLayer).bindTooltip(`${r.name}${rv ? ' — ' + rv : ''} — scan corridor along river flow`));
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
  const demoBtn = document.getElementById('demo-btn');
  if (demoBtn) {
    const hasDemo = rivers.some(isTestRiver);
    demoBtn.dataset.has = hasDemo ? '1' : '0';
    demoBtn.textContent = hasDemo ? '✕ Remove test data' : '＋ Add test river';
  }
  if (rivers.length === 0) {
    el.innerHTML = '<div class="empty-state">No rivers added yet — search above.</div>';
    return;
  }
  el.innerHTML = rivers.map(r => {
    const lastScan = r.last_scanned_at ? timeAgo(r.last_scanned_at) : 'never';
    const enabled = r.monitoring_enabled !== false;
    const points = r.monitor_points ? r.monitor_points.length : 0;
    return `
      <div class="alert-item rv-row">
        <div class="rv-info">
          <strong>${escapeHtml(r.name)}</strong>${isTestRiver(r) ? '<span class="test-badge">TEST</span>' : ''}${r.area_kind === 'live_area' ? '<span class="area-badge">📍 LIVE AREA</span>' : ''}
          <div class="meta">${escapeHtml(r.district || r.state || '')}${points ? ` · ${points} corridor points` : ''} · scans every ${r.scan_interval_days || 5}d · last scan: ${lastScan}</div>
          ${r.area_kind === 'live_area' ? `<div class="meta">📍 ${escapeHtml(r.place_name || '')}${r.radius_km ? ` · radius ${r.radius_km} km` : ''}${r.river_names ? ` · rivers: ${escapeHtml(r.river_names)}` : ''}</div>` : ''}
        </div>
        <div class="rv-actions">
          <button class="btn" onclick="focusRiver('${r.id}')">Show</button>
          <a href="/details" class="btn" onclick="sessionStorage.setItem('openRiver','${r.id}')">Details</a>
          <button class="btn" onclick="triggerScanNow('${r.id}', this)">Scan now</button>
          <button class="btn ${enabled ? 'primary' : ''}" onclick="toggleMonitoring('${r.id}', ${!enabled})">${enabled ? 'Monitoring ON' : 'Monitoring OFF'}</button>
          <button class="btn danger" onclick="deleteRiver('${r.id}')">Delete</button>
        </div>
      </div>
    `;
  }).join('');
}

function focusRiverBounds(r) {
  return r && r.radius_km ? L.latLng(r.center_lat, r.center_lng).toBounds(r.radius_km * 2000) : null;
}

function focusRiver(riverId) {
  const r = riverMarkers[riverId];
  if (!r) return;
  const b = focusRiverBounds(r);
  if (b) map.flyToBounds(b, { duration: 0.8 }); else map.flyTo([r.center_lat, r.center_lng], 11, { duration: 0.8 });
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

// ── Search | Live switch ──────────────────────────────────────────────
// Search: find a river by name (as before). Live: paste a Google Maps link / coordinates -> real place name + rivers within a radius.
let searchMode = 'search';
let livePreview = null;
const previewLayer = L.layerGroup().addTo(map);
const modeBtns = [...document.querySelectorAll('#mode-switch .mode-btn')];

function clearLivePreview() { previewLayer.clearLayers(); livePreview = null; }

function setSearchMode(mode) {
  searchMode = mode;
  modeBtns.forEach(b => { const on = b.dataset.mode === mode; b.classList.toggle('active', on); b.setAttribute('aria-pressed', on ? 'true' : 'false'); });
  document.getElementById('live-options').hidden = mode !== 'live';
  document.getElementById('live-hint').hidden = mode !== 'live';
  searchInput.placeholder = mode === 'live' ? 'Paste a Google Maps link or coordinates, e.g. 14.4644, 75.9218' : "Search a river, e.g. 'Tungabhadra river Davangere'";
  searchBtn.textContent = mode === 'live' ? 'Find area' : 'Search';
  searchResults.innerHTML = '';
  clearLivePreview();
}
modeBtns.forEach(b => b.addEventListener('click', () => setSearchMode(b.dataset.mode)));
const runSearch = () => (searchMode === 'live' ? doLivePreview() : doRiverSearch());
searchBtn.addEventListener('click', runSearch);
searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });

const RIVER_COLORS = ['#4FD1C5', '#7C9CFF', '#F6AD55'];
function drawLivePreview(d) {
  previewLayer.clearLayers();
  const circle = L.circle([d.lat, d.lng], { radius: d.radius_km * 1000, color: '#7C9CFF', weight: 2, dashArray: '6 6', fillOpacity: 0.05 }).addTo(previewLayer);
  L.circleMarker([d.lat, d.lng], { radius: 8, color: '#fff', weight: 2, fillColor: '#E4572E', fillOpacity: 1 }).addTo(previewLayer).bindTooltip(d.place_name, { permanent: true, direction: 'top' });
  d.rivers.forEach((r, i) => {
    const c = RIVER_COLORS[i % RIVER_COLORS.length];
    L.polyline(r.points.map(p => [p.lat, p.lng]), { color: c, weight: 4, opacity: 0.9 }).addTo(previewLayer).bindTooltip(`${r.name} · ${r.distance_km} km away`);
    r.points.forEach(p => L.circleMarker([p.lat, p.lng], { radius: 4, color: c, fillColor: c, fillOpacity: 0.9, weight: 1 }).addTo(previewLayer));
  });
  map.flyToBounds(circle.getBounds().pad(0.08), { duration: 0.8 });
}

function renderLivePreview(d) {
  const src = { google_maps: 'Google Maps link', google_short_link: 'Google Maps short link', coordinates: 'coordinates' }[d.source] || 'location';
  const custom = document.getElementById('live-name').value.trim();
  const rivers = d.rivers.length
    ? `<div class="meta" style="margin-top:8px;">Rivers found within ${d.radius_km} km — these will be monitored:</div>
       <ul class="live-rivers">${d.rivers.map((r, i) => `<li><span style="color:${RIVER_COLORS[i % RIVER_COLORS.length]}">●</span> <strong>${escapeHtml(r.name)}</strong>
         <span class="meta">${r.distance_km} km away · ${r.length_km} km inside the radius · ${r.points.length} monitoring points</span></li>`).join('')}</ul>`
    : `<div class="live-warn">${escapeHtml(d.message || 'No river found in this radius.')}</div>`;
  searchResults.innerHTML = `
    <div class="live-card">
      <div class="live-place">📍 <strong>${escapeHtml(d.place_name)}</strong></div>
      <div class="meta">${d.lat.toFixed(5)}, ${d.lng.toFixed(5)} · from ${src} · correct place name found automatically</div>
      ${custom ? `<div class="meta" style="margin-top:4px;">Will be saved as: <strong style="color:var(--text);">${escapeHtml(custom)}</strong></div>` : '<div class="meta" style="margin-top:4px;">Tip: type your own place name above before saving (optional).</div>'}
      ${(d.warnings || []).map(w => `<div class="live-warn">⚠ ${escapeHtml(w)}</div>`).join('')}
      ${rivers}
      <div class="live-actions">
        <button class="btn primary" id="live-save-btn" ${d.rivers.length ? '' : 'disabled'}>Save &amp; start monitoring</button>
        <button class="btn" id="live-cancel-btn">Cancel</button>
      </div>
    </div>`;
  document.getElementById('live-cancel-btn').onclick = () => { searchResults.innerHTML = ''; clearLivePreview(); };
  document.getElementById('live-save-btn').onclick = doLiveSave;
}

async function doLivePreview() {
  const text = searchInput.value.trim();
  if (!text) { searchResults.innerHTML = '<div class="empty-state">Paste a Google Maps link or coordinates first.</div>'; return; }
  const radius = Number(document.getElementById('live-radius').value);
  searchResults.innerHTML = `<div class="empty-state"><span class="spinner"></span> Finding the place and the rivers within ${radius} km…</div>`;
  clearLivePreview();
  try {
    const res = await apiFetch('/api/live-location/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ input: text, radius_km: radius }) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { searchResults.innerHTML = `<div class="empty-state" style="color:var(--accent-high);">${escapeHtml(errorText(data, res.status))}</div>`; return; }
    livePreview = data;
    drawLivePreview(data);
    renderLivePreview(data);
  } catch (e) {
    searchResults.innerHTML = '<div class="empty-state" style="color:var(--accent-high);">Could not reach the server — check your internet connection.</div>';
  }
}

async function doLiveSave() {
  if (!livePreview) return;
  const btn = document.getElementById('live-save-btn');
  btn.disabled = true; btn.textContent = 'Saving…';
  const body = { lat: livePreview.lat, lng: livePreview.lng, radius_km: livePreview.radius_km, input: searchInput.value.trim(), name: document.getElementById('live-name').value.trim() };
  try {
    const res = await apiFetch('/api/live-location', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      searchResults.innerHTML = `<div class="empty-state" style="color:var(--accent-high);">${escapeHtml(errorText(data, res.status))}</div>`;
      return;
    }
    const n = (data.river.monitor_points || []).length;
    searchResults.innerHTML = `<div class="empty-state" style="color:var(--accent-teal);">Saved <strong>${escapeHtml(data.river.name)}</strong> (📍 ${escapeHtml(data.place_name)}) — monitoring ${n} points on
      ${data.rivers.length} river${data.rivers.length === 1 ? '' : 's'} within ${data.radius_km} km.</div>${(data.warnings || []).map(w => `<div class="live-warn">⚠ ${escapeHtml(w)}</div>`).join('')}`;
    searchInput.value = ''; document.getElementById('live-name').value = '';
    clearLivePreview();
    await loadRivers();
    await refreshAll();
    const b = focusRiverBounds(data.river);
    if (b) map.flyToBounds(b, { duration: 0.8 });
  } catch (e) {
    searchResults.innerHTML = '<div class="empty-state" style="color:var(--accent-high);">Could not save — check your internet connection.</div>';
  }
}

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
    const isTest = signals.demo_test === true;
    const dataLabel = isTest ? '🧪 TEST DATA — simulated alert for demonstration, not a real detection'
      : isReal ? `✓ real Sentinel-2: ${escapeHtml(signals.before_date || '?')} → ${escapeHtml(signals.after_date || '?')}` : (isDemo ? '⚠ SYNTHETIC demo data — not a real observation (GEE not connected)' : '');
    const corridorInfo = signals.corridor_points_scanned ? ` · ${signals.corridor_points_scanned} corridor points scanned` : '';
    return `
    <div class="alert-item" onclick="focusAlert(${a.lat}, ${a.lng})">
      <span class="severity-badge ${a.severity}">${a.severity}</span>${isTest ? '<span class="test-badge">TEST</span>' : ''}
      <span style="font-family: var(--font-mono); font-size: 11.5px; color: var(--text-muted); margin-left: 8px;">risk ${(a.confidence || 0).toFixed(0)}/100</span>
      <div class="meta">${a.source === 'satellite' ? '🛰 satellite' : '📹 manual upload'} · ${timeAgo(a.detected_at)}${corridorInfo}</div>
      ${dataLabel ? `<div class="meta" style="color:${isReal ? 'var(--accent-teal)' : 'var(--accent-watch)'};">${dataLabel}</div>` : ''}
      ${!(a.before_image_url && a.after_image_url) && a.annotated_image_path ? `<div style="margin-top:8px;"><img src="${a.annotated_image_path}" alt="detection image" loading="lazy" style="width:100%; border-radius:6px; border:1px solid var(--border);"><div class="meta" style="text-align:center;">detection image${isTest ? ' (simulated illustration)' : ''}</div></div>` : ''}
      ${a.evidence_pdf_url && !(a.before_image_url && a.after_image_url) ? `<div class="meta" style="margin-top:6px;"><a href="/api/alerts/${a.id}/evidence/verify" target="_blank" rel="noopener">✔ verify hash</a></div>` : ''}
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
    const popup = `<strong>${signals.demo_test === true ? '🧪 TEST · ' : ''}${a.severity}</strong> (risk ${(a.confidence || 0).toFixed(0)}/100)<br>${escapeHtml(a.reasoning_text || '')}`;

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
    if (!st.write_protection) notes.push('ℹ Write protection is off (RIVERGUARD_API_KEY not set).');
    el.style.display = notes.length ? 'block' : 'none';
    el.innerHTML = notes.map(escapeHtml).join('<br>');
  } catch { el.style.display = 'none'; }
}

// ── Test river ("demo") helpers ─────────────────────────────────────
function isTestRiver(r) { return /\(Test\)\s*$/.test(String((r && r.name) || '')); }

async function toggleDemoData() {
  const btn = document.getElementById('demo-btn');
  const hasDemo = btn.dataset.has === '1';
  if (hasDemo && !confirm('Remove the TEST river and its simulated alert?\n\nReal rivers and real alerts are not touched.')) return;
  btn.disabled = true;
  try {
    if (hasDemo) {
      const res = await apiFetch('/api/demo', { method: 'DELETE' });
      if (!res.ok) { const d = await res.json().catch(() => ({})); alert(errorText(d, res.status, 'Could not remove test data.')); return; }
    } else {
      btn.textContent = 'Mapping the real river…';
      const res = await apiFetch('/api/demo/seed', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { alert(errorText(data, res.status, 'Could not add test data.')); return; }
      await loadRivers();
      await refreshAll();
      if (data.alert && data.alert.lat != null) map.flyTo([data.alert.lat, data.alert.lng], 13, { duration: 0.8 });
      if (data.warnings && data.warnings.length) alert('Test data added, but:\n\n' + data.warnings.join('\n'));
      return;
    }
    await loadRivers();
    await refreshAll();
  } catch (e) {
    alert(e.message || 'Test data request failed.');
  } finally {
    btn.disabled = false;
    btn.textContent = btn.dataset.has === '1' ? '✕ Remove test data' : '＋ Add test river';
  }
}
document.getElementById('demo-btn').addEventListener('click', toggleDemoData);

// ── Full-screen map ─────────────────────────────────────────────────
const mapPanel = document.getElementById('map-panel');
const fsBtn = document.getElementById('map-fs-btn');

function setMapFull(on) {
  mapPanel.classList.toggle('map-fullscreen', on);
  document.body.classList.toggle('no-scroll', on);
  fsBtn.textContent = on ? '✕ Exit full screen' : '⤢ Full screen';
  fsBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  [60, 300].forEach(ms => setTimeout(() => map.invalidateSize(), ms));   // let the browser finish resizing, then redraw tiles
}

fsBtn.addEventListener('click', () => {
  const on = !mapPanel.classList.contains('map-fullscreen');
  setMapFull(on);                                                       // CSS full-screen works everywhere (incl. iPhone Safari)
  if (on && mapPanel.requestFullscreen) mapPanel.requestFullscreen().catch(() => {});   // plus real browser full screen where supported
  if (!on && document.fullscreenElement) document.exitFullscreen().catch(() => {});
});
document.addEventListener('fullscreenchange', () => {
  if (!document.fullscreenElement && mapPanel.classList.contains('map-fullscreen')) setMapFull(false);
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && mapPanel.classList.contains('map-fullscreen')) setMapFull(false);
});

loadSystemStatus();
loadRivers();
refreshAll();
setInterval(refreshAll, 30000);