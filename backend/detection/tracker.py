"""
A small, dependency-light multi-object tracker (IOU + centroid matching).
This is intentionally simple rather than pulling in ByteTrack/DeepSORT as
hard dependencies — it's enough to give each truck/JCB/person a stable
track_id across frames so we can measure dwell time and movement, which is
exactly what the behavior/reasoning layer needs.

If you want a stronger tracker later, drop in the `supervision` package's
ByteTrack and swap it in here — TrackedObject's shape stays the same so
nothing downstream needs to change.
"""
from dataclasses import dataclass, field


def iou(box_a, box_b):
    xa1, ya1, xa2, ya2 = box_a
    xb1, yb1, xb2, yb2 = box_b
    inter_x1, inter_y1 = max(xa1, xb1), max(ya1, yb1)
    inter_x2, inter_y2 = min(xa2, xb2), min(ya2, yb2)
    inter_w, inter_h = max(0, inter_x2 - inter_x1), max(0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h
    area_a = (xa2 - xa1) * (ya2 - ya1)
    area_b = (xb2 - xb1) * (yb2 - yb1)
    union = area_a + area_b - inter_area
    return inter_area / union if union > 0 else 0.0


@dataclass
class Track:
    track_id: int
    class_name: str
    bbox: tuple
    first_frame: int
    last_frame: int
    positions: list = field(default_factory=list)  # [(frame_number, cx, cy)]
    misses: int = 0


class SimpleTracker:
    def __init__(self, iou_threshold=0.3, max_misses=8):
        self.iou_threshold = iou_threshold
        self.max_misses = max_misses
        self.tracks = {}
        self._next_id = 1

    def update(self, frame_number, detections):
        """
        detections: list of dicts with 'class_name', 'bbox' (x1,y1,x2,y2), 'confidence'.
        Returns the same list, each dict annotated with a 'track_id'.
        """
        unmatched = list(range(len(detections)))
        active_ids = [tid for tid, t in self.tracks.items() if t.misses <= self.max_misses]

        for tid in active_ids:
            track = self.tracks[tid]
            best_iou, best_idx = 0, None
            for idx in unmatched:
                det = detections[idx]
                if det["class_name"] != track.class_name:
                    continue
                score = iou(track.bbox, det["bbox"])
                if score > best_iou:
                    best_iou, best_idx = score, idx

            if best_idx is not None and best_iou >= self.iou_threshold:
                det = detections[best_idx]
                track.bbox = det["bbox"]
                track.last_frame = frame_number
                track.misses = 0
                cx = (det["bbox"][0] + det["bbox"][2]) / 2
                cy = (det["bbox"][1] + det["bbox"][3]) / 2
                track.positions.append((frame_number, cx, cy))
                det["track_id"] = tid
                unmatched.remove(best_idx)
            else:
                track.misses += 1

        for idx in unmatched:
            det = detections[idx]
            tid = self._next_id
            self._next_id += 1
            cx = (det["bbox"][0] + det["bbox"][2]) / 2
            cy = (det["bbox"][1] + det["bbox"][3]) / 2
            self.tracks[tid] = Track(
                track_id=tid, class_name=det["class_name"], bbox=det["bbox"],
                first_frame=frame_number, last_frame=frame_number,
                positions=[(frame_number, cx, cy)],
            )
            det["track_id"] = tid

        return detections

    def finalize(self, fps):
        """
        Call once all frames are processed. Returns a per-track behavior
        summary: dwell time, distance travelled, and a crude "convoy"/
        "loitering" classification — the raw material for the reasoning engine.
        """
        summaries = []
        for tid, t in self.tracks.items():
            if len(t.positions) < 1:
                continue
            dwell_frames = t.last_frame - t.first_frame + 1
            dwell_seconds = dwell_frames / fps if fps > 0 else 0
            total_dist = 0.0
            for i in range(1, len(t.positions)):
                _, x1, y1 = t.positions[i - 1]
                _, x2, y2 = t.positions[i]
                total_dist += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5

            behavior = "loitering" if total_dist < 40 and dwell_seconds > 5 else "moving"
            summaries.append({
                "track_id": tid,
                "class_name": t.class_name,
                "dwell_seconds": round(dwell_seconds, 1),
                "pixel_distance_travelled": round(total_dist, 1),
                "behavior": behavior,
                "num_detections": len(t.positions),
            })
        return summaries
