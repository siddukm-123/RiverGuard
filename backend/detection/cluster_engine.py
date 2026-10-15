"""
Cluster engine - groups nearby detections into one "incident".

  * cluster_pixels(): clusters detection-box centres inside ONE frame (image-space, pixels).
    Pixel coordinates are image coordinates - they are never interpreted as geography.
  * cluster_per_frame(): runs cluster_pixels() independently on every video frame. (The first
    version clustered all frames together, so a single parked truck seen in 3 sampled frames
    formed a "cluster of 3" and was read as an active work site.)
  * cluster_geo(): clusters real lat/lng points (metres) - used only when detections carry GPS.
  * summarize_clusters(): per-cluster summary counting UNIQUE objects (by track_id when present).

eps is scaled with image width (reference: 120 px at 1280 px width, as in the paper) so the same
physical grouping distance is used regardless of camera resolution.
"""
import numpy as np

REFERENCE_WIDTH_PX = 1280.0
EVIDENCE_ROLES = ("evidence",)


def scaled_eps(frame_width, base_eps=120.0):
    if not frame_width:
        return base_eps
    return base_eps * (float(frame_width) / REFERENCE_WIDTH_PX)


def cluster_geo(detections, eps_m=150, min_samples=2):
    """detections: dicts with 'lat','lng'. Adds 'cluster_id' (-1 = noise). Uses haversine DBSCAN."""
    from sklearn.cluster import DBSCAN
    if not detections:
        return detections
    coords = np.radians(np.array([[d["lat"], d["lng"]] for d in detections]))
    labels = DBSCAN(eps=eps_m / 6371000.0, min_samples=min_samples, metric="haversine").fit_predict(coords)
    for d, label in zip(detections, labels):
        d["cluster_id"] = int(label)
    return detections


def cluster_pixels(detections, eps_px=None, min_samples=2, frame_width=None):
    """Cluster box centres of detections from ONE frame. Adds 'cluster_id' (-1 = noise)."""
    from sklearn.cluster import DBSCAN
    if not detections:
        return detections
    eps = eps_px if eps_px is not None else scaled_eps(frame_width)
    centroids = np.array([[(d["bbox"][0] + d["bbox"][2]) / 2, (d["bbox"][1] + d["bbox"][3]) / 2] for d in detections])
    labels = DBSCAN(eps=eps, min_samples=min_samples).fit_predict(centroids)
    for d, label in zip(detections, labels):
        d["cluster_id"] = int(label)
    return detections


def cluster_per_frame(detections, frame_width=None, min_samples=2):
    """Cluster each video frame separately. cluster_id becomes unique per (frame, local cluster)."""
    by_frame = {}
    for d in detections:
        by_frame.setdefault(d.get("frame_number", 0), []).append(d)
    next_global = 0
    for _, dets in sorted(by_frame.items()):
        cluster_pixels(dets, frame_width=frame_width, min_samples=min_samples)
        local = {}
        for d in dets:
            cid = d["cluster_id"]
            if cid == -1:
                continue
            if cid not in local:
                local[cid] = next_global
                next_global += 1
            d["cluster_id"] = local[cid]
    return detections


def summarize_clusters(detections):
    """
    One summary per cluster. `count` = unique objects (distinct track_id when tracked, else boxes).
    Only detections with role 'evidence' (person / truck / jcb) feed the flags used by the scorer;
    context classes (car, bus) are listed but never counted as evidence.
    """
    clusters = {}
    for d in detections:
        cid = d.get("cluster_id", -1)
        if cid == -1:
            continue
        clusters.setdefault(cid, []).append(d)

    summaries = []
    for cid, dets in clusters.items():
        ev = [d for d in dets if d.get("role", "evidence") in EVIDENCE_ROLES]
        if not ev:
            continue
        unique_keys = {(d["class_name"], d["track_id"]) if d.get("track_id") is not None else id(d) for d in ev}
        classes = sorted({d["class_name"] for d in ev})
        summaries.append({
            "cluster_id": cid,
            "classes_present": classes,
            "count": len(unique_keys),
            "avg_confidence": round(sum(d["confidence"] for d in ev) / len(ev), 3),
            "has_truck": "truck" in classes,
            "has_jcb": "jcb" in classes,
            "has_person": "person" in classes,
            "frame_number": ev[0].get("frame_number"),
        })
    return summaries
