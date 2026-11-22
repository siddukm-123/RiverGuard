function escapeHtml(str) {
  return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function parseSignals(alert) {
  let s = alert.reasoning_signals;
  if (typeof s === 'string') { try { s = JSON.parse(s); } catch { s = {}; } }
  return s || {};
}

function timeAgo(iso) {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

let detailMap = null;
let detailLayers = null;
let cachedRivers = [];

async function loadRiverList() {
  const res = await apiFetch('/api/rivers');
  const rivers = await res.json();
  cachedRivers = rivers;
  const el = document.getElementById('rivers-detail-list');
  if (!rivers.length) {
    el.innerHTML = '<div class="empty-state">No rivers added yet. Add one from the Dashboard.</div>';
    return;
  }
  el.innerHTML = rivers.map(r => {
    const pts = (r.monitor_points || []).length;
    const last = r.last_scanned_at ? timeAgo(r.last_scanned_at) : 'never';
    return `
      <div class="river-row">
        <div>
          <strong>${escapeHtml(r.name)}</strong>
          <div class="meta">${r.area_kind === 'live_area' ? '📍 ' + escapeHtml(r.place_name || '') + (r.radius_km ? ' · radius ' + r.radius_km + ' km' : '') + ' · ' : escapeHtml(r.district || r.state || '') + ' · '}${pts} on-river bed points · last scan: ${last}</div>
        </div>
        <div style="display:flex; gap:8px;">
          <button class="btn" onclick="refreshCorridor('${r.id}', this)">Fix corridor</button>
          <button class="btn primary" onclick="showRiverDetails('${r.id}')">Details</button>
          <button class="btn danger" onclick="deleteRiver('${r.id}')">Delete</button>
        </div>
      </div>`;
  }).join('');
}

async function refreshCorridor(riverId, btn) {
  btn.textContent = 'Fixing…';
  btn.disabled = true;
  try {
    const res = await apiFetch(`/api/rivers/${riverId}/refresh-corridor`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) { alert(data.error || 'Failed'); return; }
    await loadRiverList();
    alert(`Corridor updated: ${(data.monitor_points || []).length} points along river channel only.`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Fix corridor';
  }
}

async function deleteRiver(riverId) {
  const r = cachedRivers.find(x => x.id === riverId);
  const name = r ? r.name : 'this river';
  if (!confirm(`Delete "${name}" from monitoring?\n\nThis removes the river and all its scan alerts.`)) return;
  const res = await apiFetch(`/api/rivers/${riverId}`, { method: 'DELETE' });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    alert(data.error || 'Failed to delete river.');
    return;
  }
  closeDetail();
  await loadRiverList();
}

async function showRiverDetails(riverId) {
  const res = await apiFetch(`/api/rivers/${riverId}/scan-history`);
  const data = await res.json();
  if (!res.ok) { alert(data.error || 'Failed to load'); return; }

  const river = data.river;
  const scans = data.scans || [];
  document.getElementById('detail-title').textContent = `${river.name} — complete scan history`;
  const panel = document.getElementById('detail-panel');
  panel.classList.add('open');

  const points = river.monitor_points || [];
  let html = `
    <div class="meta" style="margin-bottom:12px;">
      Center: ${river.center_lat?.toFixed(4)}, ${river.center_lng?.toFixed(4)} ·
      ${points.length} scan points along river bed (upstream → downstream) ·
      scans every ${river.scan_interval_days || 5} days
    </div>
    <div class="section-title">Monitor points (river channel only)</div>
    <table class="point-table"><thead><tr><th>#</th><th>Label</th><th>Lat</th><th>Lng</th></tr></thead><tbody>
      ${points.map((p, i) => `<tr><td>${i + 1}</td><td>${escapeHtml(p.label)}</td><td>${p.lat}</td><td>${p.lng}</td></tr>`).join('')}
    </tbody></table>
  `;

  if (!scans.length) {
    html += '<div class="empty-state" style="padding:24px;">No scans yet. Go to Dashboard and click "Scan now".</div>';
  } else {
    html += `<div class="section-title" style="margin-top:20px;">Scan history (${scans.length})</div>`;
    scans.forEach(scan => {
      const sig = parseSignals(scan);
      const beforeDate = escapeHtml(sig.before_date || '—');
      const afterDate = escapeHtml(sig.after_date || '—');
      const isReal = sig.synthetic_data === false;
      const results = sig.scan_results || [];
      html += `
        <div class="scan-card">
          <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
            <span class="severity-badge ${scan.severity}">${scan.severity}</span>
            <span class="meta">risk ${(scan.confidence || 0).toFixed(0)}/100 · ${timeAgo(scan.detected_at)}</span>
            <span class="meta" style="color:${isReal ? 'var(--accent-teal)' : 'var(--accent-watch)'};">
              ${isReal ? 'Real Sentinel-2' : 'SYNTHETIC demo data'}
            </span>
          </div>
          <div class="meta" style="margin-top:8px;">
            <strong>Satellite dates:</strong> before <code>${beforeDate}</code> → after <code>${afterDate}</code>
            · Hotspot: ${escapeHtml(sig.hotspot_label || '—')} · corridor-relative change ${((sig.satellite_change_score || 0) * 100).toFixed(0)}%
          </div>
          ${isReal ? `<div class="meta" style="margin-top:4px;">Method: ${escapeHtml(sig.method || '—')} · mask: ${escapeHtml(sig.mask_source || '—')} · usable points: ${sig.corridor_points_usable ?? '—'}/${sig.corridor_points_scanned ?? '—'} · rain between scenes: ${sig.rain_mm_between_scenes != null ? sig.rain_mm_between_scenes + ' mm' : 'n/a'}${sig.rain_confounder ? ' ⚠' : ''}${sig.background_available === false ? ' · ⚠ river-wide background NOT removed (<3 usable points)' : ''}</div>` : ''}
          <div class="reasoning" style="margin-top:8px; font-size:13px;">${escapeHtml(scan.reasoning_text || '')}</div>
          ${scan.before_image_url && scan.after_image_url ? `
            <div class="img-pair">
              <div style="flex:1;"><img src="/api/alerts/${scan.id}/before.jpg"><div class="meta" style="text-align:center;">before (${beforeDate})</div></div>
              <div style="flex:1;"><img src="/api/alerts/${scan.id}/after.jpg"><div class="meta" style="text-align:center;">after (${afterDate})</div></div>
            </div>` : ''}
          ${results.length ? `
            <table class="point-table">
              <thead><tr><th>Point</th><th>Location</th><th>Relative change</th><th>Changed bed area</th><th>NDWI diff</th><th>Scenes</th><th>Data</th></tr></thead>
              <tbody>
                ${results.map(r => {
                  const cls = (r.change_score || 0) >= 0.5 ? 'change-high' : ((r.change_score || 0) >= 0.25 ? 'change-watch' : '');
                  return `<tr>
                    <td>${escapeHtml(r.label)}</td>
                    <td>${r.lat?.toFixed(4)}, ${r.lng?.toFixed(4)}</td>
                    <td class="${cls}">${r.change_score != null ? (r.change_score * 100).toFixed(1) + '%' : escapeHtml(r.status || 'n/a')}</td>
                    <td>${r.changed_fraction != null ? (r.changed_fraction * 100).toFixed(1) + '%' : '—'}</td>
                    <td>${r.raw_ndwi_diff != null ? r.raw_ndwi_diff : '—'}</td>
                    <td>${escapeHtml(r.before_date || '—')} → ${escapeHtml(r.after_date || '—')}</td>
                    <td>${r.synthetic ? 'SYNTHETIC' : 'real'}</td>
                  </tr>`;
                }).join('')}
              </tbody>
            </table>` : ''}
          ${scan.evidence_pdf_url ? `<a href="/api/alerts/${scan.id}/evidence.pdf" target="_blank" style="font-size:12px;color:var(--accent-teal);">Download evidence PDF</a>` : ''}
        </div>`;
    });
  }

  document.getElementById('detail-content').innerHTML = html;
  renderDetailMap(river, scans);
  panel.scrollIntoView({ behavior: 'smooth' });
}

function renderDetailMap(river, scans) {
  if (detailMap) { detailMap.remove(); detailMap = null; }
  detailMap = L.map('detail-map').setView([river.center_lat, river.center_lng], 11);
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19 }).addTo(detailMap);
  detailLayers = L.layerGroup().addTo(detailMap);

  const pts = river.monitor_points || [];
  if (pts.length > 1) {
    L.polyline(pts.map(p => [p.lat, p.lng]), { color: '#4FD1C5', weight: 4 }).addTo(detailLayers);
  }
  pts.forEach((p, i) => {
    L.circleMarker([p.lat, p.lng], { radius: 6, color: '#D9A441', fillColor: '#D9A441', fillOpacity: 0.8 })
      .bindTooltip(`Bed point ${i + 1}: ${p.label}`).addTo(detailLayers);
  });

  scans.forEach(scan => {
    const sig = parseSignals(scan);
    if (scan.lat == null) return;
    const color = scan.severity === 'HIGH' ? '#E4572E' : '#D9A441';
    L.circleMarker([scan.lat, scan.lng], { radius: 9, color, fillColor: color, fillOpacity: 0.7 })
      .bindPopup(`Hotspot: ${sig.hotspot_label || scan.severity}`).addTo(detailLayers);
    (sig.scan_results || []).forEach(r => {
      if (r.lat == null) return;
      L.circleMarker([r.lat, r.lng], { radius: 4, color: '#D9A441', fillOpacity: 0.4, weight: 1, fillColor: '#D9A441' })
        .addTo(detailLayers);
    });
  });
  setTimeout(() => detailMap.invalidateSize(), 200);
}

function closeDetail() {
  document.getElementById('detail-panel').classList.remove('open');
}

loadRiverList();
(function () {
  const openId = sessionStorage.getItem('openRiver');
  if (openId) {
    sessionStorage.removeItem('openRiver');
    showRiverDetails(openId);
  }
})();


// ── Manual detections: files uploaded on the Manual Detection page, saved with a name and their result ──
const savedById = {};

function sdSummary(d) {
  const parts = Object.entries(d.object_counts || {}).map(([k, v]) => `${escapeHtml(k)} ×${v}`);
  return parts.length ? parts.join(' · ') : 'No trucks or people detected';
}

function renderSaved(d) {
  savedById[d.id] = d;
  const when = d.detected_at ? new Date(d.detected_at).toLocaleString() : '';
  const where = d.river_name ? `River: ${escapeHtml(d.river_name)}` : (d.lat != null ? `GPS ${(+d.lat).toFixed(4)}, ${(+d.lng).toFixed(4)}` : 'No location given');
  const dur = d.file_kind === 'video' && d.video_info && d.video_info.duration_s ? ` · ${d.video_info.duration_s} s` : '';
  const view = d.has_video ? `<button class="btn primary" onclick="sdPlay('${d.id}')">▶ Play with boxes</button>`
    : (d.image_url ? `<button class="btn primary" onclick="sdPlay('${d.id}')">🖼 View detection</button>` : '');
  return `
    <div class="sd-card" id="sd-${d.id}">
      <div class="sd-head">
        <div class="sd-title"><span class="sd-icon">${d.file_kind === 'video' ? '🎞' : '🖼'}</span>
          <div style="min-width:0;"><strong class="sd-name">${escapeHtml(d.name)}</strong>${d.is_test ? '<span class="test-badge">TEST</span>' : ''}
            <div class="meta">${escapeHtml(d.original_filename || '')}${dur} · ${escapeHtml(when)} · ${where}</div></div></div>
        <div class="sd-badges"><span class="severity-badge ${escapeHtml(d.severity || '')}">${escapeHtml(d.severity || '')}</span>
          <span class="meta">risk ${(d.confidence || 0).toFixed(0)}/100</span></div>
      </div>
      <div class="sd-result"><strong>Result:</strong> ${sdSummary(d)}</div>
      <details class="sd-more"><summary>Full explanation</summary><div class="meta" style="margin-top:6px; line-height:1.5;">${escapeHtml(d.reasoning_text || '')}</div></details>
      <div class="sd-actions">${view}
        ${d.pdf_url ? `<a class="btn" href="${d.pdf_url}" target="_blank" rel="noopener">↓ Evidence PDF</a>` : ''}
        <button class="btn" onclick="sdRename('${d.id}')">✎ Rename</button>
        <button class="btn danger" onclick="sdDelete('${d.id}')">Delete</button></div>
      <div class="sd-player" id="sd-player-${d.id}" hidden></div>
    </div>`;
}

async function loadSavedDetections() {
  const box = document.getElementById('saved-list');
  const count = document.getElementById('saved-count');
  try {
    const res = await apiFetch('/api/detections');
    const list = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(list.error || `Request failed (${res.status})`);
    count.textContent = list.length ? `${list.length} saved` : '';
    box.innerHTML = list.length ? list.map(renderSaved).join('')
      : '<div class="empty-state">No saved detections yet. Upload a video or image on the Manual Detection page and it appears here with its name and result.</div>';
  } catch (e) {
    box.innerHTML = `<div class="empty-state" style="color:var(--accent-high);">Could not load saved detections — ${escapeHtml(e.message)}</div>`;
  }
}

async function sdPlay(id) {
  const d = savedById[id];
  const slot = document.getElementById(`sd-player-${id}`);
  if (!slot.hidden) { slot.hidden = true; slot.innerHTML = ''; return; }      // second click closes it
  slot.hidden = false;
  if (!d.has_video) { slot.innerHTML = `<img class="sd-img" src="${d.image_url}" alt="Detection image">`; return; }
  slot.innerHTML = `
    <div class="rg-vwrap"><video controls playsinline preload="metadata" src="${d.video_url}"></video><canvas></canvas></div>
    <div class="rg-note" id="sd-note-${id}">Red = truck / excavator · teal = person · dashed grey = car / bus (context). Boxes come from the frames the model analysed.</div>`;
  const video = slot.querySelector('video'), canvas = slot.querySelector('canvas');
  video.onerror = () => {
    document.getElementById(`sd-note-${id}`).innerHTML = `This browser cannot play this video format (for example .mov / .avi / H.265). <a href="${d.video_url}" download>Download it</a> instead.`;
  };
  if (d.overlay_url && window.RGOverlay) {
    try {
      const res = await apiFetch(d.overlay_url);
      if (res.ok) window.RGOverlay.attach(video, canvas, await res.json());
    } catch { /* the video still plays, just without boxes */ }
  }
}

async function sdRename(id) {
  const d = savedById[id];
  const name = window.prompt('Name for this detection:', d.name);
  if (name === null || !name.trim()) return;
  const res = await apiFetch(`/api/detections/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() }) });
  if (!res.ok) { const e = await res.json().catch(() => ({})); alert(e.error || 'Could not rename.'); return; }
  loadSavedDetections();
}

async function sdDelete(id) {
  const d = savedById[id];
  if (!window.confirm(`Delete “${d.name}” with its video, pictures and PDF? This cannot be undone.`)) return;
  const res = await apiFetch(`/api/detections/${id}`, { method: 'DELETE' });
  if (!res.ok) { const e = await res.json().catch(() => ({})); alert(e.error || 'Could not delete.'); return; }
  loadSavedDetections();
}

loadSavedDetections();