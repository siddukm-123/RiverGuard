"""Video analysis speed controls: frame cap for long videos, progress reporting, job progress endpoint."""
import threading
import time

import cv2
import numpy as np
import pytest

import config
import jobs
from detection import detector


@pytest.fixture()
def fake_video(tmp_path):
    """20 s @ 25 fps tiny video (500 frames) - decoding is instant, so tests are fast and need no YOLO weights."""
    path = tmp_path / "clip.mp4"
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 25, (96, 64))
    for i in range(500):
        frame = np.full((64, 96, 3), 40 + i % 100, np.uint8)
        w.write(frame)
    w.release()
    return str(path)


@pytest.fixture()
def stub_detector(monkeypatch):
    calls = []

    def fake(frame, mode="off", conf=None):
        calls.append(1)
        return {"detections": [], "original": [], "enhanced": None, "enhanced_frame": None,
                "lowlight": {"mode": mode, "applied": False, "reason": "stub", "enhancement": None,
                             "assessment": {"mean_luma": 80.0}}}
    monkeypatch.setattr(detector, "detect_frame_lowlight", fake)
    return calls


def test_frame_cap_lowers_sampling_for_long_videos(fake_video, stub_detector, monkeypatch):
    monkeypatch.setattr(config, "VIDEO_MAX_ANALYZED_FRAMES", 10)
    r = detector.analyze_video_file(fake_video, sample_fps=5)          # would be ~100 frames without the cap
    assert 5 <= r["frames_analyzed"] <= 10
    assert len(stub_detector) == r["frames_analyzed"]                  # YOLO really ran only on the capped frames
    assert r["sample_fps_requested"] == 5.0
    assert r["sample_fps"] < r["sample_fps_requested"]                 # honestly reported
    assert r["duration_s"] == pytest.approx(20.0, abs=0.5)             # still covers the WHOLE video, just more sparsely


def test_no_cap_keeps_requested_rate(fake_video, stub_detector, monkeypatch):
    monkeypatch.setattr(config, "VIDEO_MAX_ANALYZED_FRAMES", 400)
    r = detector.analyze_video_file(fake_video, sample_fps=5)
    assert r["frames_analyzed"] == 100 and r["sample_fps"] == r["sample_fps_requested"] == 5.0


def test_cap_zero_disables_cap(fake_video, stub_detector, monkeypatch):
    monkeypatch.setattr(config, "VIDEO_MAX_ANALYZED_FRAMES", 0)
    assert detector.analyze_video_file(fake_video, sample_fps=5)["frames_analyzed"] == 100


def test_progress_callback_is_monotonic_and_never_reaches_one(fake_video, stub_detector):
    seen = []
    detector.analyze_video_file(fake_video, sample_fps=5, progress_cb=seen.append)
    assert len(seen) == 100
    assert seen == sorted(seen) and seen[0] >= 0 and seen[-1] < 1.0     # 1.0 is set by the job when it is really done


def test_failing_progress_callback_does_not_break_analysis(fake_video, stub_detector):
    def boom(_):
        raise RuntimeError("ui bug")
    assert detector.analyze_video_file(fake_video, sample_fps=5, progress_cb=boom)["frames_analyzed"] == 100


def test_job_progress_is_exposed_through_the_api(client):
    started, release = threading.Event(), threading.Event()

    def work():
        jobs.report_progress(0.42)
        started.set()
        release.wait(5)
        return {"ok": True}

    jid = jobs.submit("test", work)
    assert started.wait(5)
    mid = client.get(f"/api/jobs/{jid}").get_json()
    assert mid["status"] == "running" and mid["progress"] == 0.42
    release.set()
    for _ in range(50):
        done = client.get(f"/api/jobs/{jid}").get_json()
        if done["status"] == "done":
            break
        time.sleep(0.05)
    assert done["status"] == "done" and done["progress"] == 1.0 and done["result"] == {"ok": True}


def test_report_progress_outside_a_job_is_ignored():
    jobs.report_progress(0.5)  # must not raise
