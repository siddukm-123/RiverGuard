"""
Lightweight multi-object tracker (same-class IoU association, globally greedy).

Intentionally dependency-free. It gives each truck/excavator/person a stable
`track_id` across *sampled* frames so that dwell time and movement can be measured.
It is NOT ByteTrack/DeepSORT: no motion model, no re-identification after a long
occlusion - an object that disappears for more than `max_misses` sampled frames and
re-appears gets a NEW id, so `unique tracked objects` is an upper-bound estimate.

Fixes vs. the first version
  * Association is globally greedy on IoU (best pair first) instead of "first track wins".
  * Dwell time uses the VIDEO frame rate (frame indices are raw video frames); the old
    code divided raw frame counts by the *sampling* fps and inflated dwell ~15x.
  * Loitering is judged relative to the object's own size and needs >= 3 observations,
    instead of an absolute 40 px threshold that depends on camera resolution.
"""
from dataclasses import dataclass, field


def iou(box_a, box_b):
    xa1, ya1, xa2, ya2 = box_a
    xb1, yb1, xb2, yb2 = box_b
    inter_w = max(0, min(xa2, xb2) - max(xa1, xb1))
    inter_h = max(0, min(ya2, yb2) - max(ya1, yb1))
    inter = inter_w * inter_h
    union = (xa2 - xa1) * (ya2 - ya1) + (xb2 - xb1) * (yb2 - yb1) - inter
    return inter / union if union > 0 else 0.0


@dataclass
class Track:
    track_id: int
    class_name: str
    bbox: tuple
    first_frame: int
    last_frame: int
    positions: list = field(default_factory=list)  # [(frame_number, cx, cy)]
    sizes: list = field(default_factory=list)      # bbox diagonal at each observation
    confidences: list = field(default_factory=list)
    misses: int = 0


class SimpleTracker:
    def __init__(self, iou_threshold=0.3, max_misses=8):
        self.iou_threshold = iou_threshold
        self.max_misses = max_misses
        self.tracks = {}
        self._next_id = 1

    @staticmethod
    def _center(b):
        return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2

    @staticmethod
    def _diag(b):
        return ((b[2] - b[0]) ** 2 + (b[3] - b[1]) ** 2) ** 0.5

    def update(self, frame_number, detections, frame_size=None):
        """Annotates each detection dict with 'track_id' and returns the list."""
        active = [tid for tid, t in self.tracks.items() if t.misses <= self.max_misses]

        pairs = []
        for tid in active:
            t = self.tracks[tid]
            for idx, det in enumerate(detections):
                if det["class_name"] != t.class_name:
                    continue
                score = iou(t.bbox, det["bbox"])
                if score >= self.iou_threshold:
                    pairs.append((score, tid, idx))
        pairs.sort(reverse=True)

        matched_tracks, matched_dets = set(), set()
        for score, tid, idx in pairs:
            if tid in matched_tracks or idx in matched_dets:
                continue
            matched_tracks.add(tid)
            matched_dets.add(idx)
            det = detections[idx]
            t = self.tracks[tid]
            t.bbox, t.last_frame, t.misses = det["bbox"], frame_number, 0
            cx, cy = self._center(det["bbox"])
            t.positions.append((frame_number, cx, cy))
            t.sizes.append(self._diag(det["bbox"]))
            t.confidences.append(det["confidence"])
            det["track_id"] = tid

        for tid in active:
            if tid not in matched_tracks:
                self.tracks[tid].misses += 1

        for idx, det in enumerate(detections):
            if idx in matched_dets:
                continue
            tid = self._next_id
            self._next_id += 1
            cx, cy = self._center(det["bbox"])
            self.tracks[tid] = Track(
                track_id=tid, class_name=det["class_name"], bbox=det["bbox"],
                first_frame=frame_number, last_frame=frame_number,
                positions=[(frame_number, cx, cy)], sizes=[self._diag(det["bbox"])],
                confidences=[det["confidence"]],
            )
            det["track_id"] = tid
        return detections

    def finalize(self, video_fps=None, frame_size=None, fps=None, min_observations=3,
                 loiter_seconds=5.0, loiter_move_ratio=0.5):
        """
        Per-track behaviour summary.

        video_fps: real frame rate of the source video (frame numbers are raw video frames).
        `fps` is accepted for backwards compatibility and treated as video_fps.
        behaviour = 'loitering' when seen >= min_observations times, for >= loiter_seconds,
        while its centre moved less than `loiter_move_ratio` x its own bbox diagonal.
        Otherwise 'moving' (moved more) or 'brief' (too few observations to judge).
        """
        video_fps = video_fps or fps or 25.0
        out = []
        for tid, t in self.tracks.items():
            if not t.positions:
                continue
            dwell_s = (t.last_frame - t.first_frame) / video_fps
            dist = 0.0
            for i in range(1, len(t.positions)):
                _, x1, y1 = t.positions[i - 1]
                _, x2, y2 = t.positions[i]
                dist += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            mean_size = sum(t.sizes) / len(t.sizes)
            # net displacement start->end is robust to jitter; path length is kept for reporting
            _, sx, sy = t.positions[0]
            _, ex, ey = t.positions[-1]
            net = ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5
            if len(t.positions) < min_observations:
                behavior = "brief"
            elif dwell_s >= loiter_seconds and net <= loiter_move_ratio * mean_size and dist <= 2.0 * mean_size:
                behavior = "loitering"
            else:
                behavior = "moving"
            out.append({
                "track_id": tid,
                "class_name": t.class_name,
                "first_seen_s": round(t.first_frame / video_fps, 2),
                "last_seen_s": round(t.last_frame / video_fps, 2),
                "dwell_seconds": round(dwell_s, 1),
                "pixel_distance_travelled": round(dist, 1),
                "net_displacement_px": round(net, 1),
                "mean_bbox_diagonal_px": round(mean_size, 1),
                "behavior": behavior,
                "num_detections": len(t.positions),
                "mean_confidence": round(sum(t.confidences) / len(t.confidences), 3),
            })
        return out
