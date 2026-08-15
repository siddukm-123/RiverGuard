const uploadZone = document.getElementById('upload-zone');
const fileInput = document.getElementById('file-input');
const submitBtn = document.getElementById('submit-btn');
const statusText = document.getElementById('status-text');
const resultArea = document.getElementById('result-area');
const riverSelect = document.getElementById('river-select');

let selectedFile = null;

async function loadRiverOptions() {
  const res = await apiFetch('/api/rivers');
  const rivers = await res.json();
  rivers.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = `${r.name} (${r.district || r.state || ''})`;
    riverSelect.appendChild(opt);
  });
}
loadRiverOptions();

uploadZone.addEventListener('click', () => fileInput.click());
uploadZone.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('dragover'); });
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('dragover'));
uploadZone.addEventListener('drop', e => {
  e.preventDefault();
  uploadZone.classList.remove('dragover');
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', e => {
  if (e.target.files.length) handleFile(e.target.files[0]);
});

function handleFile(file) {
  selectedFile = file;
  uploadZone.querySelector('div').innerHTML = `<strong>${file.name}</strong>`;
  submitBtn.disabled = false;
}

submitBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  submitBtn.disabled = true;
  statusText.innerHTML = '<span class="spinner"></span> Running detection… this can take a while for video.';
  resultArea.innerHTML = '';

  const formData = new FormData();
  formData.append('file', selectedFile);
  if (riverSelect.value) formData.append('river_id', riverSelect.value);
  const lat = document.getElementById('lat-input').value;
  const lng = document.getElementById('lng-input').value;
  if (lat) formData.append('lat', lat);
  if (lng) formData.append('lng', lng);

  try {
    const res = await apiFetch('/api/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Detection failed');
    renderResult(data);
    statusText.textContent = 'Done.';
  } catch (err) {
    statusText.textContent = '';
    resultArea.innerHTML = `<div class="reasoning-block" style="border-left-color:#E4572E;">Error: ${err.message}</div>`;
  } finally {
    submitBtn.disabled = false;
  }
});

function renderResult(alert) {
  const severityColors = { HIGH: '#E4572E', WATCH: '#D9A441', SAFE: '#3FA796' };
  const color = severityColors[alert.severity] || '#7E9296';

  const detectionChips = (alert.detections || []).slice(0, 40).map(d =>
    `<span class="detection-chip">${d.class_name} ${(d.confidence * 100).toFixed(0)}%${d.track_id != null ? ` · #${d.track_id}` : ''}</span>`
  ).join('');

  resultArea.innerHTML = `
    <div class="section-title">Result</div>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom: 10px;">
      <span class="severity-badge ${alert.severity}" style="font-size:13px; padding:5px 12px;">${alert.severity}</span>
      <span style="font-family: var(--font-mono); color: var(--text-muted); font-size: 13px;">${alert.confidence}% confidence</span>
    </div>
    <div class="reasoning-block" style="border-left-color: ${color};">${alert.reasoning_text}</div>
    ${detectionChips ? `<div class="section-title">Detections (${(alert.detections || []).length})</div><div>${detectionChips}</div>` : ''}
    ${alert.annotated_image_path ? `<div class="section-title">Annotated frame</div><img class="result-image" src="/api/alerts/${alert.id}/annotated.jpg?t=${Date.now()}">` : ''}
    <div style="margin-top:16px;">
      <a href="/api/alerts/${alert.id}/evidence.pdf" target="_blank" class="btn primary" style="text-decoration:none; display:inline-block;">Download evidence PDF</a>
    </div>
  `;
}
