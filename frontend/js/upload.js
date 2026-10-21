const uploadZone = document.getElementById('upload-zone');
const fileInput = document.getElementById('file-input');
const submitBtn = document.getElementById('submit-btn');
const statusText = document.getElementById('status-text');
const resultArea = document.getElementById('result-area');
const riverSelect = document.getElementById('river-select');
const lowlightSelect = document.getElementById('lowlight-select');
const previewArea = document.getElementById('preview-area');

const IMAGE_EXT = ['.jpg', '.jpeg', '.png', '.bmp'];
const VIDEO_EXT = ['.mp4', '.avi', '.mov', '.mkv'];
let selectedFile = null;
let previewUrl = null;
let serverMaxMb = 200;

function esc(v) {
  return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function extOf(name) { const i = name.lastIndexOf('.'); return i < 0 ? '' : name.slice(i).toLowerCase(); }

async function loadRiverOptions() {
  try {
    const res = await apiFetch('/api/rivers');
    const rivers = await res.json();
    rivers.forEach(r => {
      const opt = document.createElement('option');
      opt.value = r.id;
      opt.textContent = `${r.name} (${r.district || r.state || ''})`;
      riverSelect.appendChild(opt);
    });
  } catch { /* river list is optional context */ }
}

async function loadModelStatus() {
  const el = document.getElementById('model-status');
  try {
    const res = await apiFetch('/api/system-status');
    const st = await res.json();
    serverMaxMb = (st.limits && st.limits.max_upload_mb) || serverMaxMb;
    const m = st.model || {};
    el.innerHTML = `
      <div>Model: <code>${esc(m.base_model_path)}</code> ${m.base_model_loaded ? '<span style="color:var(--accent-teal)">(loaded)</span>' : '(loads on first detection)'}</div>
      <div>Confidence threshold: ${esc(m.conf_threshold)}</div>
      <div>Excavator / JCB: ${m.excavator_detection_available
        ? `<span style="color:var(--accent-teal)">available</span> (${esc((m.excavator_classes_recognised || []).join(', '))})`
        : '<span style="color:var(--accent-watch)">NOT available</span> — the default COCO model has no excavator class'}</div>
      <div>Storage: ${st.storage_persistent ? 'persistent database' : '<span style="color:var(--accent-watch)">in-memory (lost on restart)</span>'}</div>
      <div>Alerts: WhatsApp ${st.notifications && st.notifications.whatsapp_configured ? 'configured' : 'not configured'}, e-mail ${st.notifications && st.notifications.email_configured ? 'configured' : 'not configured'}</div>`;
  } catch {
    el.textContent = 'Status unavailable (could not reach the API).';
  }
}

loadRiverOptions();
loadModelStatus();

uploadZone.addEventListener('click', () => fileInput.click());
uploadZone.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('dragover'); });
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('dragover'));
uploadZone.addEventListener('drop', e => {
  e.preventDefault();
  uploadZone.classList.remove('dragover');
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', e => { if (e.target.files.length) handleFile(e.target.files[0]); });

function handleFile(file) {
  const ext = extOf(file.name);
  resultArea.innerHTML = '';
  if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
  previewArea.innerHTML = '';
  if (!IMAGE_EXT.includes(ext) && !VIDEO_EXT.includes(ext)) {
    selectedFile = null; submitBtn.disabled = true;
    resultArea.innerHTML = `<div class="rg-err">Unsupported file type “${esc(ext || file.name)}”. Use ${IMAGE_EXT.concat(VIDEO_EXT).join(', ')}.</div>`;
    return;
  }
  if (file.size > serverMaxMb * 1024 * 1024) {
    selectedFile = null; submitBtn.disabled = true;
    resultArea.innerHTML = `<div class="rg-err">File is ${(file.size / 1048576).toFixed(1)} MB; the server limit is ${serverMaxMb} MB.</div>`;
    return;
  }
  selectedFile = file;
  uploadZone.querySelector('div:nth-child(2)').innerHTML = `<strong>${esc(file.name)}</strong> · ${(file.size / 1048576).toFixed(1)} MB`;
  previewUrl = URL.createObjectURL(file);
  previewArea.innerHTML = IMAGE_EXT.includes(ext)
    ? `<div class="section-title">Preview</div><img class="result-image" style="max-height:260px;object-fit:contain;" src="${previewUrl}" alt="Selected image preview">`
    : `<div class="section-title">Preview</div><video class="result-image" style="max-height:260px;" src="${previewUrl}" controls muted></video>`;
  submitBtn.disabled = false;
}

submitBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  const isVideo = VIDEO_EXT.includes(extOf(selectedFile.name));
  submitBtn.disabled = true;
  resultArea.innerHTML = '';
  statusText.innerHTML = `<span class="spinner"></span> ${isVideo ? 'Starting upload…' : 'Uploading…'}`;

  const fd = new FormData();
  fd.append('file', selectedFile);
  fd.append('lowlight', lowlightSelect.value);
  if (riverSelect.value) fd.append('river_id', riverSelect.value);
  const lat = document.getElementById('lat-input').value;
  const lng = document.getElementById('lng-input').value;
  if (lat !== '' || lng !== '') {
    if (lat === '' || lng === '') {
      statusText.textContent = '';
      resultArea.innerHTML = '<div class="rg-err">Enter both latitude and longitude, or leave both empty.</div>';
      submitBtn.disabled = false;
      return;
    }
    fd.append('lat', lat); fd.append('lng', lng);
  }
  if (isVideo) fd.append('async', '1');

  const t0 = Date.now();
  const mb = n => (n / 1048576).toFixed(1);
  const bar = pct => `<span class="rg-progress"><span style="width:${pct}%"></span></span>`;
  const onUpload = (sent, total) => {
    const pct = Math.min(100, Math.round(sent / total * 100));
    const speed = sent / 1048576 / Math.max(0.5, (Date.now() - t0) / 1000);
    statusText.innerHTML = pct >= 100
      ? `<span class="spinner"></span> Upload complete — saving the file and ${isVideo ? 'queuing the analysis' : 'running detection'}…`
      : `${bar(pct)} Uploading ${pct}% · ${mb(sent)} of ${mb(total)} MB · ${speed.toFixed(1)} MB/s`;
  };

  try {
    const res = await apiUpload('/api/upload', fd, onUpload);
    let data = await res.json().catch(() => ({}));
    if (!res.ok && res.status !== 202) {
      const why = [data.error || `Request failed (${res.status})`, data.detail && data.detail !== data.error ? `— ${data.detail}` : '', data.ref ? `(ref ${data.ref})` : '']
        .filter(Boolean).join(' ');
      throw new Error(why);
    }
    if (res.status === 202) {
      data = await pollJob(data.job_id, (job, secs) => {
        const pct = Math.round((job.progress || 0) * 100);
        statusText.innerHTML = job.status === 'queued'
          ? `<span class="spinner"></span> Waiting for the analyser to start… ${secs}s`
          : `${bar(pct)} Analysing video ${pct}% · ${secs}s`;
      });
    }
    renderResult(data);
    statusText.textContent = 'Done.';
  } catch (err) {
    statusText.textContent = '';
    resultArea.innerHTML = `<div class="rg-err"><strong>Detection failed.</strong> ${esc(err.message)}</div>`;
  } finally {
    submitBtn.disabled = false;
  }
});

function statCard(k, v) { return `<div class="rg-stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`; }

function renderResult(a) {
  const color = { HIGH: '#E4572E', WATCH: '#D9A441', SAFE: '#3FA796' }[a.severity] || '#7E9296';
  const an = a.analysis || {};
  const sig = a.reasoning_signals || {};
  const dets = a.detections || [];
  const counts = a.object_counts || {};
  const isVideo = !!(an.video || sig.video);
  const rs = an.reasoning || sig.reasoning || {};
  const mi = an.model_info || {};
  const ll = an.lowlight || {};
  const cmp = an.lowlight_comparison;
  const media = (sig.media_available || []);
  const prov = an.provenance || {};

  const countText = Object.entries(counts).map(([k, v]) => `${k}: ${v}`).join(' · ');
  const noObjects = Object.keys(counts).length === 0;

  const breakdown = (rs.breakdown || []).map(b =>
    `<tr><td>${b.applied ? '+' + b.points : '0'}</td><td>${esc(b.signal)}${b.note ? ` <span class="meta">— ${esc(b.note)}</span>` : ''}</td></tr>`).join('');

  const detRows = dets.slice(0, 100).map(d => `<tr>
      <td>${esc(d.class_name)}${d.role === 'context' ? ' <span class="meta">(context)</span>' : ''}</td>
      <td>${(d.confidence * 100).toFixed(0)}%</td>
      <td>${d.track_id != null ? '#' + esc(d.track_id) : '—'}</td>
      <td>${d.timestamp_s != null ? esc(d.timestamp_s) + ' s' : '—'}</td>
      <td>${esc(d.view || 'original')}</td></tr>`).join('');

  const trackRows = (a.track_behaviors || []).slice(0, 40).map(t => `<tr>
      <td>#${esc(t.track_id)}</td><td>${esc(t.class_name)}</td><td>${esc(t.first_seen_s)}–${esc(t.last_seen_s)} s</td>
      <td>${esc(t.dwell_seconds)} s</td><td>${esc(t.behavior)}</td><td>${esc(t.num_detections)}</td></tr>`).join('');

  const imgSrc = kind => `/api/alerts/${encodeURIComponent(a.id)}/image/${kind}?t=${Date.now()}`;
  const tabs = [];
  if (media.includes('annotated')) tabs.push(['annotated', isVideo ? 'Annotated frame' : 'Annotated']);
  if (media.includes('comparison')) tabs.push(['comparison', 'Original vs enhanced']);
  if (media.includes('original') && !isVideo) tabs.push(['original', 'Original']);
  if (media.includes('enhanced')) tabs.push(['enhanced', 'Enhanced']);

  const llApplied = ll.applied || (ll.frames_enhanced || 0) > 0;
  const lowlightHtml = (ll.mode && ll.mode !== 'off') ? `
    <div class="section-title">Low-light processing</div>
    <div class="rg-note">Mode: <strong>${esc(ll.mode)}</strong> · ${isVideo
      ? `${esc(ll.frames_enhanced)} of ${esc(ll.frames_analyzed)} sampled frames enhanced (mean luma min ${esc(ll.mean_luma_min)}, avg ${esc(ll.mean_luma_avg)})`
      : `${llApplied ? 'enhancement applied' : 'not applied'} — ${esc(ll.reason)}`}</div>
    ${!isVideo && ll.assessment ? `<div class="rg-grid">${statCard('Mean luma', ll.assessment.mean_luma)}${statCard('Noise σ', ll.assessment.noise_sigma)}${statCard('Sharpness', ll.assessment.blur_laplacian_var)}${statCard('Dark pixels', (ll.assessment.dark_pixel_fraction * 100).toFixed(0) + '%')}</div>` : ''}
    ${cmp ? `<table class="rg-table"><thead><tr><th>View</th><th>Detections</th></tr></thead><tbody>
        <tr><td>Original</td><td>${esc(JSON.stringify(cmp.original_counts))}</td></tr>
        <tr><td>Enhanced</td><td>${esc(JSON.stringify(cmp.enhanced_counts))}</td></tr>
        <tr><td>Merged (used for scoring)</td><td>${esc(JSON.stringify(cmp.merged_counts))}</td></tr></tbody></table>
      <div class="rg-note">${esc(cmp.note)}</div>` : ''}
    ${isVideo && llApplied ? `<div class="rg-note">Detections seen in the original view: ${esc(ll.detections_in_original_view)}; only in the enhanced view: ${esc(ll.detections_only_in_enhanced_view)}. More detections are not proof of better accuracy.</div>` : ''}
    <div class="rg-warn">Software enhancement is <strong>not</strong> night vision. It cannot create information the sensor did not capture and can amplify noise into false detections.</div>` : '';

  resultArea.innerHTML = `
    <div class="section-title">Result</div>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom: 10px; flex-wrap:wrap;">
      <span class="severity-badge ${esc(a.severity)}" style="font-size:13px; padding:5px 12px;">${esc(a.severity)}</span>
      <span style="font-family: var(--font-mono); color: var(--text-muted); font-size: 13px;">risk score ${esc(a.confidence)}/100</span>
      <span class="meta">${esc(rs.method || 'rule_based')}</span>
    </div>
    <div class="reasoning-block" style="border-left-color: ${color};">${esc(a.reasoning_text)}</div>
    <div class="rg-note">The risk score is a hand-set, rule-based prior — not a calibrated probability. A detection is a signal for field verification, not proof of illegal mining.</div>

    <div class="rg-grid">
      ${statCard(isVideo ? 'Unique objects tracked' : 'Objects detected', noObjects ? 0 : Object.values(counts).reduce((x, y) => x + y, 0))}
      ${statCard(isVideo ? 'Detections (all frames)' : 'Boxes', a.detections_total ?? dets.length)}
      ${isVideo ? statCard('Frames analysed', (an.video || {}).frames_analyzed ?? '—') : ''}
      ${isVideo ? statCard('Duration', ((an.video || {}).duration_s ?? '—') + ' s') : ''}
    </div>
    <div class="rg-note">${noObjects
      ? 'No truck, excavator or person was detected above the confidence threshold. This is an empty result, not proof that nothing is there.'
      : esc(countText)}</div>
    ${isVideo ? '<div class="rg-note">“Unique objects” counts tracker IDs — an object that disappears for several seconds and returns gets a new ID, so this is an upper estimate.</div>' : ''}
    ${(an.video || {}).truncated ? `<div class="rg-warn">Video was longer than the ${esc((an.video || {}).max_seconds)} s analysis limit; only the first part was analysed.</div>` : ''}
    ${mi.excavator_detection_available === false ? '<div class="rg-warn">Excavator/JCB detection is <strong>not available</strong> with the loaded model, so excavators will not appear here even if present.</div>' : ''}
    ${(a.warnings || []).map(w => `<div class="rg-warn">${esc(w)}</div>`).join('')}

    ${isVideo && a.video_overlay && a.video_overlay.frames && a.video_overlay.frames.length && previewUrl ? `
      <div class="section-title">Video with detection boxes</div>
      <div class="rg-vwrap"><video id="rg-video" src="${previewUrl}" controls muted playsinline></video><canvas id="rg-overlay"></canvas></div>
      <div class="rg-note">Press play. <span style="color:#E4572E">■ truck / excavator</span> · <span style="color:#4FD1C5">■ person</span> · <span style="color:#9FB0B3">▢ car / bus (context only, dashed)</span>.
        Boxes are drawn from the frames the model analysed (${esc((a.video_overlay.sample_fps || '?'))} per second); between those frames a tracked box is moved smoothly, nothing extra is detected.
        ${a.video_overlay.truncated ? 'Box list was capped, so the end of a very long video may have no boxes.' : ''}</div>` : ''}

    ${tabs.length ? `<div class="rg-tabs" id="img-tabs">${tabs.map((t, i) => `<button class="btn ${i === 0 ? 'active' : ''}" data-kind="${t[0]}">${esc(t[1])}</button>`).join('')}</div>
      <img class="result-image" id="result-img" src="${imgSrc(tabs[0][0])}" alt="Detection result image">` : ''}

    ${dets.length ? `<div class="section-title">Detections (${a.detections_total ?? dets.length}${dets.length < (a.detections_total ?? dets.length) ? `, first ${dets.length} shown` : ''})</div>
      <table class="rg-table"><thead><tr><th>Class</th><th>Conf.</th><th>Track</th><th>Time</th><th>View</th></tr></thead><tbody>${detRows}</tbody></table>` : ''}

    ${trackRows ? `<div class="section-title">Tracked objects</div>
      <table class="rg-table"><thead><tr><th>ID</th><th>Class</th><th>Seen</th><th>Dwell</th><th>Behaviour</th><th>Hits</th></tr></thead><tbody>${trackRows}</tbody></table>` : ''}

    ${lowlightHtml}

    <details class="rg-details"><summary>How the risk score was built</summary>
      <table class="rg-table"><tbody>${breakdown || '<tr><td colspan="2">No signals applied.</td></tr>'}</tbody></table>
      ${(rs.caps || []).map(c => `<div class="rg-note">• ${esc(c)}</div>`).join('')}
      <div class="rg-note">Zone check: ${esc(an.zone_method || 'n/a')} · location source: ${esc(an.location_source || 'none')}</div>
    </details>

    <details class="rg-details"><summary>Provenance</summary>
      <div class="rg-note">Model <code>${esc(mi.base_model)}</code> (ultralytics ${esc(mi.framework_version)}), threshold ${esc(mi.conf_threshold)}</div>
      <div class="rg-note">Source file: ${esc(prov.source_name)} · ${esc(prov.source_bytes)} bytes</div>
      <div class="rg-pre">SHA-256 ${esc(prov.source_sha256)}</div>
      <div class="rg-note">Notification: ${esc(an.notification || 'n/a')}</div>
    </details>

    <div style="margin-top:16px; display:flex; gap:10px; flex-wrap:wrap;">
      ${a.evidence_pdf_url ? `<a href="/api/alerts/${encodeURIComponent(a.id)}/evidence.pdf" target="_blank" rel="noopener" class="btn primary" style="text-decoration:none; display:inline-block;">Download evidence PDF</a>
      <a href="/api/alerts/${encodeURIComponent(a.id)}/evidence/verify" target="_blank" rel="noopener" class="btn" style="text-decoration:none; display:inline-block;">Verify PDF hash</a>` : '<span class="meta">Evidence PDF was not generated.</span>'}
    </div>`;

  const vEl = document.getElementById('rg-video'), cEl = document.getElementById('rg-overlay');
  if (vEl && cEl && window.RGOverlay) {
    previewArea.innerHTML = '';   // the result player below replaces the plain preview (same file, same blob URL)
    window.RGOverlay.attach(vEl, cEl, a.video_overlay);
  }

  const tabsEl = document.getElementById('img-tabs');
  if (tabsEl) {
    tabsEl.addEventListener('click', e => {
      const btn = e.target.closest('button[data-kind]');
      if (!btn) return;
      tabsEl.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
      document.getElementById('result-img').src = imgSrc(btn.dataset.kind);
    });
  }
}