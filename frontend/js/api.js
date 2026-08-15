/** API helper — uses same-origin /api when Vercel rewrites to Railway; set __API_BASE__ if needed. */
window.__API_BASE__ = window.__API_BASE__ || '';

window.apiFetch = function apiFetch(path, options) {
  const base = (window.__API_BASE__ || '').replace(/\/$/, '');
  const url = `${base}${path.startsWith('/') ? path : `/${path}`}`;
  return fetch(url, options);
};
