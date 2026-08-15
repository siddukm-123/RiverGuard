"""
Cluster engine — adapted from the sand-mining-detection zip's
cluster_engine.py. Groups nearby detections into a single "incident"
instead of reporting each box separately. Works in two modes:
  - geo mode: cluster by real GPS distance (satellite detections)
  - pixel mode: cluster by pixel distance within one frame (manual
    image/video uploads with no GPS attached)
"""
import numpy as np


def cluster_geo(detections, eps_m=150, min_samples=2):
    """
    detections: list of dicts with 'lat', 'lng'.
    Returns detections annotated with a 'cluster_id' (or -1 for noise).
    """
    from sklearn.cluster import DBSCAN
    coords = np.array([[d["lat"], d["lng"]] for d in detections])
    # Convert eps from metres to approximate degrees (fine at small distances)
    eps_deg = eps_m / 111320.0
    labels = DBSCAN(eps=eps_deg, min_samples=min_samples).fit_predict(coords)
    for d, label in zip(detections, labels):
        d["cluster_id"] = int(label)
    return detections


def cluster_pixels(detections, eps_px=120, min_samples=2):
    """
    detections: list of dicts with 'bbox' = (x_min, y_min, x_max, y_max).
    Clusters by the centroid of each box. Used for uploaded images/video
    where there's no GPS, so we group detections that are physically close
    together in the frame (e.g. a truck + JCB + person at the same pit).
    """
    from sklearn.cluster import DBSCAN
    if not detections:
        return detections
    centroids = np.array([
        [(d["bbox"][0] + d["bbox"][2]) / 2, (d["bbox"][1] + d["bbox"][3]) / 2]
        for d in detections
    ])
    labels = DBSCAN(eps=eps_px, min_samples=min_samples).fit_predict(centroids)
    for d, label in zip(detections, labels):
        d["cluster_id"] = int(label)
    return detections


def summarize_clusters(detections):
    """
    Turns clustered detections into a per-cluster summary:
    which classes are present, how many, and the average confidence.
    A cluster containing truck + jcb + person together is a much stronger
    signal than any one of those alone — this is what the reasoning engine
    reads first.
    """
    clusters = {}
    for d in detections:
        cid = d.get("cluster_id", -1)
        clusters.setdefault(cid, {"detections": [], "classes": set()})
        clusters[cid]["detections"].append(d)
        clusters[cid]["classes"].add(d["class_name"])

    summaries = []
    for cid, info in clusters.items():
        if cid == -1:
            continue  # noise points, not part of any group
        dets = info["detections"]
        summaries.append({
            "cluster_id": cid,
            "classes_present": sorted(info["classes"]),
            "count": len(dets),
            "avg_confidence": round(sum(d["confidence"] for d in dets) / len(dets), 3),
            "has_truck": "truck" in info["classes"],
            "has_jcb": "jcb" in info["classes"],
            "has_person": "person" in info["classes"],
        })
    return summaries
