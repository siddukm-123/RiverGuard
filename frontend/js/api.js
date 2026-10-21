/** API helper — same-origin /api when Vercel rewrites to Railway; set window.__API_BASE__ to call Railway directly. */
window.__API_BASE__ = window.__API_BASE__ || '';

function rgKey() { try { return sessionStorage.getItem('rgKey') || ''; } catch { return ''; } }

window.apiFetch = async function apiFetch(path, options) {
  const base = (window.__API_BASE__ || '').replace(/\/$/, '');
  const url = `${base}${path.startsWith('/') ? path : `/${path}`}`;
  const opts = Object.assign({}, options || {});
  const method = (opts.method || 'GET').toUpperCase();
  const send = () => {
    const headers = Object.assign({}, opts.headers || {});
    if (method !== 'GET' && rgKey()) headers['X-API-Key'] = rgKey();
    return fetch(url, Object.assign({}, opts, { headers }));
  };
  let res = await send();
  // Only servers that set RIVERGUARD_API_KEY ever answer 401: ask once, remember for this tab, retry.
  if (res.status === 401 && method !== 'GET') {
    const key = window.prompt('This RiverGuard server requires an access key for changes. Enter it:');
    if (key) {
      try { sessionStorage.setItem('rgKey', key); } catch { /* private mode */ }
      res = await send();
    }
  }
  return res;
};

/**
 * Upload a FormData with REAL progress (fetch cannot report upload progress).
 * onProgress(bytesSent, bytesTotal). Resolves like fetch(): a Response (ok / status / json()).
 */
window.apiUpload = async function apiUpload(path, formData, onProgress) {
  const base = (window.__API_BASE__ || '').replace(/\/$/, '');
  const url = `${base}${path.startsWith('/') ? path : `/${path}`}`;
  const once = () => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    if (rgKey()) xhr.setRequestHeader('X-API-Key', rgKey());
    xhr.upload.onprogress = e => { if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total); };
    xhr.onload = () => resolve(new Response(xhr.responseText, { status: xhr.status, headers: { 'Content-Type': 'application/json' } }));
    xhr.onerror = () => reject(new Error('The upload was interrupted (network error). Check your internet connection and try again.'));
    xhr.ontimeout = () => reject(new Error('The upload timed out. Try again, or use a smaller / shorter video.'));
    xhr.send(formData);
  });
  let res = await once();
  if (res.status === 401) {   // same one-time access-key prompt as apiFetch
    const key = window.prompt('This RiverGuard server requires an access key for changes. Enter it:');
    if (key) {
      try { sessionStorage.setItem('rgKey', key); } catch { /* private mode */ }
      res = await once();
    }
  }
  return res;
};

/** Poll /api/jobs/<id> until done. Resolves with the job result; rejects with Error(message). */
window.pollJob = async function pollJob(jobId, onTick, intervalMs = 2000, timeoutMs = 15 * 60 * 1000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    await new Promise(r => setTimeout(r, intervalMs));
    let res;
    try { res = await apiFetch(`/api/jobs/${jobId}`); } catch { continue; }  // transient network error: keep polling
    const job = await res.json().catch(() => ({}));
    if (res.status === 404) throw new Error(job.error || 'The job was lost (server restarted). Please try again.');
    if (onTick) onTick(job, Math.round((Date.now() - started) / 1000));
    if (job.status === 'done') return job.result;
    if (job.status === 'error') throw new Error(job.error || 'The job failed.');
  }
  throw new Error('Timed out waiting for the job to finish.');
};