"""Tests frontend/js/video-overlay.js (box lookup + drawing maths) by running it under node. Skipped when node is absent."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[2] / "frontend" / "js" / "video-overlay.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

HARNESS = r"""
const { boxesAt, attach } = require(%(js)s);
const frames = [
  { t: 0.0, boxes: [[100,100,200,200,'truck',0.9,1,'evidence'], [10,10,20,20,'person',0.8,2,'evidence']] },
  { t: 1.0, boxes: [[200,100,300,200,'truck',0.9,1,'evidence']] },
  { t: 5.0, boxes: [[0,0,10,10,'car',0.6,null,'context']] },
];
const out = {};
out.before_first = boxesAt(frames, -0.5, 1.5).length;
out.at_first = boxesAt(frames, 0.0, 1.5).map(b => b.cls);
const mid = boxesAt(frames, 0.5, 1.5);
out.mid_truck = mid.find(b => b.cls === 'truck').box;          // tracked id 1 -> halfway between the two samples
out.mid_person = mid.find(b => b.cls === 'person').box;        // no match in next sample -> held, not moved
out.after_hold = boxesAt(frames, 2.6, 1.5).length;             // > 1.5 s after the last sample before the gap -> gone
out.within_hold = boxesAt(frames, 2.4, 1.5).map(b => b.cls);
out.last_sample = boxesAt(frames, 5.2, 1.5).map(b => b.cls);
out.empty = boxesAt([], 1, 1).length;

// drawing maths: 1920x1080 video shown in an 800x600 element (letterboxed top/bottom)
const calls = [];
const ctx = { clearRect(){}, strokeRect(x,y,w,h){ calls.push([x,y,w,h]); }, fillRect(){}, fillText(){}, setLineDash(){}, measureText(t){ return { width: t.length * 6 }; } };
const canvas = { width: 0, height: 0, getContext(){ return ctx; } };
const video = { clientWidth: 800, clientHeight: 600, videoWidth: 1920, videoHeight: 1080, currentTime: 0, paused: true, ended: false, addEventListener(){} };
global.window = global.window || { addEventListener(){} };
attach(video, canvas, { width: 1920, height: 1080, sample_fps: 2, frames: [{ t: 0, boxes: [[0,0,192,108,'truck',0.9,null,'evidence']] }] });
out.draw = calls[0];
out.canvas_size = [canvas.width, canvas.height];
console.log(JSON.stringify(out));
"""


def _run():
    script = HARNESS % {"js": json.dumps(str(JS))}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_box_lookup_and_tracking():
    o = _run()
    assert o["before_first"] == 0 and o["empty"] == 0
    assert o["at_first"] == ["truck", "person"]
    assert o["mid_truck"] == [150, 100, 250, 200]            # moved halfway to its next position
    assert o["mid_person"] == [10, 10, 20, 20]               # unmatched box is held in place
    assert o["within_hold"] == ["truck"]                     # t=2.4: last seen 1.4 s ago (< 1.5 s hold) -> still shown, held in place
    assert o["after_hold"] == 0                              # t=2.6: 1.6 s ago -> removed, nothing lingers on screen
    assert o["last_sample"] == ["car"]


def test_boxes_land_on_the_right_pixels_when_the_video_is_letterboxed():
    o = _run()
    assert o["canvas_size"] == [800, 600]
    x, y, w, h = o["draw"]
    assert x == pytest.approx(0) and y == pytest.approx(75)  # (600 - 1080*0.4167)/2 black bar above the picture
    assert w == pytest.approx(80) and h == pytest.approx(45)  # 192x108 box scaled by 800/1920
