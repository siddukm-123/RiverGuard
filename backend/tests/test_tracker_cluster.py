from detection.tracker import SimpleTracker
from detection import cluster_engine as ce


def _det(cls, box, conf=0.9, **kw):
    return {"class_name": cls, "bbox": box, "confidence": conf, "role": "evidence", **kw}


def test_track_ids_stable_across_frames():
    t = SimpleTracker()
    ids = []
    for f in (0, 15, 30, 45):
        d = t.update(f, [_det("truck", (100 + f, 100, 200 + f, 180))])
        ids.append(d[0]["track_id"])
    assert len(set(ids)) == 1


def test_new_object_gets_new_id_and_expired_track_not_reused():
    t = SimpleTracker(max_misses=2)
    a = t.update(0, [_det("truck", (0, 0, 50, 50))])[0]["track_id"]
    for f in (1, 2, 3, 4):
        t.update(f, [])
    b = t.update(5, [_det("truck", (0, 0, 50, 50))])[0]["track_id"]
    assert a != b  # documented limitation: re-appearing after expiry is a new id


def test_global_greedy_matching_prevents_id_swap():
    t = SimpleTracker()
    first = t.update(0, [_det("person", (0, 0, 40, 100)), _det("person", (60, 0, 100, 100))])
    ida, idb = first[0]["track_id"], first[1]["track_id"]
    second = t.update(1, [_det("person", (62, 0, 102, 100)), _det("person", (2, 0, 42, 100))])
    assert second[0]["track_id"] == idb and second[1]["track_id"] == ida


def test_dwell_uses_video_fps_not_sample_fps():
    """REGRESSION: old code divided raw frame indices by the sampling fps (2) -> ~15x inflated dwell."""
    t = SimpleTracker()
    for f in range(0, 90, 15):  # 6 sampled frames over 75 raw frames @30fps = 2.5 s
        t.update(f, [_det("truck", (100, 100, 200, 180))])
    s = t.finalize(video_fps=30.0)[0]
    assert abs(s["dwell_seconds"] - 2.5) < 0.1
    assert s["behavior"] != "loitering"  # 2.5 s is not "loitering" (needs >= 5 s)


def test_loitering_needs_time_observations_and_little_movement():
    t = SimpleTracker()
    for f in range(0, 30 * 8, 15):  # 8 s, stationary
        t.update(f, [_det("truck", (100, 100, 200, 180))])
    assert t.finalize(video_fps=30.0)[0]["behavior"] == "loitering"
    t2 = SimpleTracker()
    t2.update(0, [_det("truck", (100, 100, 200, 180))])
    t2.update(300, [_det("truck", (100, 100, 200, 180))])
    assert t2.finalize(video_fps=30.0)[0]["behavior"] == "brief"  # only 2 observations -> cannot judge


def test_per_frame_clustering_does_not_merge_same_object_across_time():
    """REGRESSION: one parked truck in 3 sampled frames used to form a 'cluster of 3' (active work site)."""
    dets = []
    for f in (0, 15, 30):
        dets.append(_det("truck", (100, 100, 200, 180), frame_number=f, track_id=1))
    ce.cluster_per_frame(dets, frame_width=1280)
    assert ce.summarize_clusters(dets) == []  # a lone truck per frame is noise, not a cluster


def test_cluster_counts_unique_tracks_and_flags():
    dets = [_det("truck", (100, 100, 200, 180), frame_number=0, track_id=1),
            _det("person", (210, 120, 240, 200), frame_number=0, track_id=2),
            _det("jcb", (150, 90, 260, 190), frame_number=0, track_id=3)]
    ce.cluster_per_frame(dets, frame_width=1280)
    s = ce.summarize_clusters(dets)[0]
    assert s["count"] == 3 and s["has_truck"] and s["has_jcb"] and s["has_person"]


def test_context_vehicles_never_count_as_evidence():
    dets = [_det("bus", (100, 100, 200, 180), role="context"), _det("person", (150, 100, 190, 180))]
    ce.cluster_pixels(dets, frame_width=1280)
    s = ce.summarize_clusters(dets)[0]
    assert s["has_truck"] is False and s["classes_present"] == ["person"]


def test_cluster_eps_scales_with_resolution():
    assert ce.scaled_eps(2560) == 240 and ce.scaled_eps(1280) == 120 and ce.scaled_eps(None) == 120
