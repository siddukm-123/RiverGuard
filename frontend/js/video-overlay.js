/**
 * Draws the detector's boxes on top of the playing video (canvas over <video>).
 * Data (`video_overlay`): { width, height, sample_fps, frames: [{ t, boxes: [[x1,y1,x2,y2,class,conf,trackId|null,role], ...] }] } in original video pixels.
 * Between two analysed frames a box with the same tracker id is moved smoothly (display only - nothing is invented); an unmatched box is held
 * for at most ~1.5 sample intervals and then removed.
 */
(function (root) {
  const COLORS = { truck: '#E4572E', jcb: '#E4572E', person: '#4FD1C5', car: '#9FB0B3', bus: '#9FB0B3' };

  function boxesAt(frames, t, hold) {
    if (!frames || !frames.length) return [];
    let lo = 0, hi = frames.length - 1, i = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (frames[mid].t <= t) { i = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    if (i < 0) return [];
    const prev = frames[i], next = frames[i + 1];
    if (t - prev.t > hold) return [];
    const canBlend = next && (next.t - prev.t) <= hold * 1.2 && next.t > prev.t;
    return prev.boxes.map(b => {
      let box = [b[0], b[1], b[2], b[3]];
      if (canBlend && b[6] != null) {
        const m = next.boxes.find(n => n[6] === b[6]);
        if (m) {
          const k = (t - prev.t) / (next.t - prev.t);
          box = [0, 1, 2, 3].map(j => b[j] + (m[j] - b[j]) * k);
        }
      }
      return { box, cls: b[4], conf: b[5], id: b[6], role: b[7] };
    });
  }

  function attach(video, canvas, ov) {
    const frames = (ov && ov.frames) || [];
    const hold = 1.5 / Math.max(0.05, (ov && ov.sample_fps) || 1);
    let raf = null;
    function draw() {
      const cw = video.clientWidth, ch = video.clientHeight;
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, cw, ch);
      const vw = video.videoWidth || ov.width, vh = video.videoHeight || ov.height;
      if (!vw || !vh) return;
      const scale = Math.min(cw / vw, ch / vh);
      const ox = (cw - vw * scale) / 2, oy = (ch - vh * scale) / 2;
      const k = (vw * scale) / (ov.width || vw);
      ctx.lineWidth = 2;
      ctx.font = '12px sans-serif';
      for (const d of boxesAt(frames, video.currentTime, hold)) {
        const color = COLORS[d.cls] || '#FFFFFF';
        const x = ox + d.box[0] * k, y = oy + d.box[1] * k;
        const w = (d.box[2] - d.box[0]) * k, h = (d.box[3] - d.box[1]) * k;
        ctx.strokeStyle = color;
        ctx.setLineDash(d.role === 'context' ? [5, 4] : []);
        ctx.strokeRect(x, y, w, h);
        const label = `${d.cls} ${Math.round(d.conf * 100)}%${d.id != null ? ' #' + d.id : ''}`;
        const tw = ctx.measureText(label).width + 8;
        ctx.setLineDash([]);
        ctx.fillStyle = color;
        ctx.fillRect(x, Math.max(0, y - 16), tw, 16);
        ctx.fillStyle = '#05100f';
        ctx.fillText(label, x + 4, Math.max(12, y - 4));
      }
    }
    function loop() { draw(); if (!video.paused && !video.ended) raf = requestAnimationFrame(loop); else raf = null; }
    function kick() { if (!raf) loop(); }
    ['play', 'playing'].forEach(ev => video.addEventListener(ev, kick));
    ['pause', 'seeked', 'seeking', 'timeupdate', 'loadedmetadata', 'loadeddata'].forEach(ev => video.addEventListener(ev, draw));
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(draw).observe(video);
    window.addEventListener('resize', draw);
    draw();
    return { redraw: draw };
  }

  const api = { boxesAt, attach, COLORS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.RGOverlay = api;
})(typeof window !== 'undefined' ? window : globalThis);