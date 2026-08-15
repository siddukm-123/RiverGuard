"""
End-to-end smoke test for RiverGuard API and core logic.
Run while the Flask server is up: python test_e2e.py
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


def api(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw[:500]}


def ok(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    line = f"  [{status}] {label}" + (f" - {detail}" if detail else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode())
    return cond


def main():
    passed = failed = 0
    results = []

    def check(label, cond, detail=""):
        nonlocal passed, failed
        if ok(label, cond, detail):
            passed += 1
        else:
            failed += 1
        results.append((label, cond, detail))

    print("\n=== RiverGuard E2E Test ===\n")

    # 1. Dashboard + static
    print("1. Server & static pages")
    try:
        with urllib.request.urlopen(BASE + "/", timeout=10) as r:
            html = r.read().decode()
        check("Dashboard loads", r.status == 200 and "RIVERGUARD" in html)
        with urllib.request.urlopen(BASE + "/detect", timeout=10) as r:
            detect = r.read().decode()
        check("Manual detection page loads", "upload" in detect.lower() or "detect" in detect.lower())
    except Exception as e:
        check("Dashboard loads", False, str(e))
        check("Manual detection page loads", False, "skipped")

    # 2. Metrics
    print("\n2. Metrics")
    code, metrics = api("GET", "/api/metrics")
    check("GET /api/metrics", code == 200, f"rivers={metrics.get('rivers_monitored')}, alerts={metrics.get('total_alerts')}")

    # 3. List rivers
    print("\n3. Rivers")
    code, rivers = api("GET", "/api/rivers")
    check("GET /api/rivers", code == 200 and isinstance(rivers, list), f"count={len(rivers)}")

    # 4. Search
    print("\n4. River search (geocoding)")
    for q in ["Tungabhadra", "Ganga", "Cauvery"]:
        code, search = api("GET", f"/api/rivers/search?q={urllib.parse.quote(q)}")
        check(f"Search '{q}'", code == 200 and len(search) > 0,
              search[0].get("display_name", "")[:60] if search else "no results")

    # 5. Add river (unique name to avoid confusion)
    print("\n5. Add river (POST + river snap + corridor)")
    code, search = api("GET", "/api/rivers/search?q=Tungabhadra")
    if search:
        candidate = search[0]
        body = {
            "name": "E2E-Test-Tungabhadra",
            "state": candidate.get("state"),
            "district": candidate.get("district"),
            "center_lat": candidate["lat"],
            "center_lng": candidate["lng"],
        }
        code, created = api("POST", "/api/rivers", body)
        snapped = (
            code == 201
            and created.get("center_lat") is not None
            and isinstance(created.get("monitor_points"), list)
        )
        check("POST /api/rivers", code == 201, f"status={code}, error={created.get('error', '')}")
        check("River snapped + monitor_points", snapped,
              f"points={len(created.get('monitor_points') or [])}, center=({created.get('center_lat'):.4f},{created.get('center_lng'):.4f})")
        check("buffer_radius_m is integer-safe", code == 201 and isinstance(created.get("buffer_radius_m"), int),
              str(created.get("buffer_radius_m")))
        test_river_id = created.get("id") if code == 201 else None
    else:
        check("POST /api/rivers", False, "no search candidate")
        test_river_id = None

    # 6. Monitoring toggle
    print("\n6. Monitoring toggle")
    if test_river_id:
        code, updated = api("PATCH", f"/api/rivers/{test_river_id}/monitoring", {"monitoring_enabled": True})
        check("PATCH monitoring ON", code == 200 and updated.get("monitoring_enabled") is True)
    else:
        check("PATCH monitoring ON", False, "skipped")

    # 7. Satellite scan
    print("\n7. Satellite scan (corridor + before/after pipeline)")
    scan_river_id = test_river_id
    if not scan_river_id and rivers:
        scan_river_id = rivers[0]["id"]
    if scan_river_id:
        code, alert = api("POST", "/api/satellite-scan", {"river_id": scan_river_id})
        signals = alert.get("reasoning_signals") or {}
        if isinstance(signals, str):
            signals = json.loads(signals)
        check("POST /api/satellite-scan", code == 201, f"severity={alert.get('severity')}, conf={alert.get('confidence')}")
        check("Corridor scan results stored", bool(signals.get("scan_results")),
              f"points={signals.get('corridor_points_scanned')}")
        check("Alert has lat/lng hotspot", alert.get("lat") is not None and alert.get("lng") is not None)
        check("Reasoning text present", bool(alert.get("reasoning_text")))
        gee_real = signals.get("synthetic_data") is False
        has_images = bool(alert.get("before_image_url") and alert.get("after_image_url"))
        check("GEE real imagery", gee_real and has_images,
              "real Sentinel-2" if (gee_real and has_images) else ("demo/synthetic — GEE IAM not configured" if signals.get("synthetic_data") else "no thumbnails"))
        test_alert_id = alert.get("id")
    else:
        check("POST /api/satellite-scan", False, "no river to scan")
        test_alert_id = None

    # 8. Alerts feed
    print("\n8. Alerts feed")
    code, alerts = api("GET", "/api/alerts")
    check("GET /api/alerts", code == 200 and isinstance(alerts, list), f"count={len(alerts)}")
    if test_alert_id:
        found = any(a["id"] == test_alert_id for a in alerts)
        check("New alert in feed", found)

    # 9. Evidence endpoints
    print("\n9. Evidence files")
    if test_alert_id:
        alert = next((a for a in alerts if a["id"] == test_alert_id), {})
        if alert.get("before_image_url"):
            try:
                with urllib.request.urlopen(BASE + f"/api/alerts/{test_alert_id}/before.jpg", timeout=15) as r:
                    check("Before image downloadable", r.status == 200 and r.headers.get("Content-Type", "").startswith("image/"))
            except Exception as e:
                check("Before image downloadable", False, str(e))
        else:
            check("Before image downloadable", False, "no before image (GEE not connected)")
        if alert.get("evidence_pdf_url"):
            try:
                with urllib.request.urlopen(BASE + f"/api/alerts/{test_alert_id}/evidence.pdf", timeout=15) as r:
                    check("Evidence PDF downloadable", r.status == 200)
            except Exception as e:
                check("Evidence PDF downloadable", False, str(e))
        else:
            check("Evidence PDF downloadable", False, "no pdf path")

    # 10. Python module checks
    print("\n10. Core modules")
    try:
        from satellite.geocode import search_place
        from satellite.river_corridor import fetch_monitor_points, snap_to_river_center
        from satellite.gee_client import is_configured
        r = search_place("Tungabhadra river Davangere")
        check("Geocode module", len(r) > 0)
        if r:
            pts = fetch_monitor_points(r[0]["lat"], r[0]["lng"], max_points=5)
            check("Corridor module", len(pts) >= 1, f"{len(pts)} points")
            slat, slng, _ = snap_to_river_center(r[0]["lat"], r[0]["lng"], max_points=5)
            check("River snap module", slat is not None)
        check("GEE configured", is_configured(), "yes" if is_configured() else "no — needs Service Usage Consumer role")
    except Exception as e:
        check("Core modules", False, str(e))

    print(f"\n=== Results: {passed} passed, {failed} failed ===\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
