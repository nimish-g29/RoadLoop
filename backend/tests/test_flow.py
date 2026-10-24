"""End-to-end loop at the service layer, plus the HTTP API, on SQLite with a fake TinyFish.

Run inside the api container:  docker compose run --rm api python -m pytest -q
(or locally: pip install -r requirements.txt -r requirements-dev.txt && python -m pytest -q)
"""
import io
import json
import os
import tempfile
from datetime import timedelta

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["MEDIA_DIR"] = f"{_tmp}/media"
os.environ["TINYFISH_MODE"] = "mock"
os.environ["GEOCODER"] = "off"

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Complaint, Hotspot, Rider  # noqa: E402
from app.services import workflow as wf  # noqa: E402

init_db()

LAT, LON = 28.54590, 77.27320  # Okhla, near IIIT Delhi


class FakeTF:
    def __init__(self):
        self.runs, self.monitors, self.deleted, self.n = {}, {}, [], 0

    def run_async(self, url, goal, schema=None, webhook_url=None, **kw):
        self.n += 1
        rid = f"run{self.n}"
        self.runs[rid] = goal
        return rid

    def create_page_monitor(self, name, url, cron, purpose=None, webhook_url=None):
        mid = f"mon{len(self.monitors) + 1}"
        self.monitors[mid] = url
        return {"monitor": {"id": mid}}

    def delete_monitor(self, mid):
        self.deleted.append(mid)

    def fetch(self, urls, **kw):
        return {"results": [{"url": u, "text": f"Complaint No: {u.rsplit('/', 1)[-1]}\nStatus: Under Process"}
                            for u in urls], "errors": []}


def filed(run_id, cid):
    return {"run_id": run_id, "status": "COMPLETED",
            "result": {"status": "filed", "complaint_id": cid, "photo_attached": True}}


def det(rider, conf=0.85, jolt=0.6, area=0.07, dlat=0.0):
    return wf.DetectionIn(rider_id=rider, lat=LAT + dlat, lon=LON, area_frac=area, model_conf=conf, jolt_g=jolt,
                          speed_kmh=25, gps_accuracy_m=5, road_name="Okhla Main Road", address="Okhla Ph 3",
                          city_key="delhi", state_key="dl")


def jobs_named(jobs, name):
    return [kw for n, kw in jobs if n == name]


@pytest.fixture()
def s():
    with SessionLocal() as session:
        yield session
        session.rollback()


def riders(s, n):
    out = []
    for i in range(n):
        r = Rider(display_name=f"Rider {i}", phone=f"98765432{i:02d}")
        s.add(r)
        out.append(r)
    s.flush()
    return [r.id for r in out]


def test_full_loop_with_fake_closure(s):
    tf = FakeTF()
    r1, r2, r3, r4 = riders(s, 4)

    d, jobs = wf.ingest_detection(s, det(r1), "x/a.jpg")
    h = s.get(Hotspot, d.hotspot_id)
    assert h.state == "candidate"
    wf.ingest_detection(s, det(r1), None)            # same rider again: still one witness
    assert h.state == "candidate"
    d2, jobs = wf.ingest_detection(s, det(r2, dlat=0.00005), None)  # ~5 m away -> same hotspot
    assert d2.hotspot_id == h.id and h.state == "confirmed" and h.severity == "high"

    with pytest.raises(wf.WorkflowError):
        wf.approve_filing(s, h.id, r3)               # r3 never saw it
    c, jobs = wf.approve_filing(s, h.id, r1)
    assert h.state == "filing" and jobs_named(jobs, "roadloop.file_complaint")

    jobs = wf.start_filing(s, c.id, tf)
    assert "Okhla Ph 3" in tf.runs["run1"] and jobs_named(jobs, "roadloop.poll_run")
    jobs = wf.handle_run(s, filed("run1", "DMG-1"))
    assert h.state == "filed" and c.portal_complaint_id == "DMG-1"
    assert wf.handle_run(s, filed("run1", "DMG-1")) == []   # webhook + poll both arrive: idempotent

    wf.start_tracking(s, c.id, tf)
    assert c.monitor_id == "mon1"
    wf.on_monitor_webhook(s, {"fetch_monitor_id": "mon1", "results": [{"text": "Complaint No: DMG-1\nStatus: Under Process"}]})
    assert h.state == "in_progress"
    jobs = wf.on_monitor_webhook(s, {"fetch_monitor_id": "mon1", "results": [{"text": "Complaint No: DMG-1\nStatus: Closed"}]})
    assert h.state == "awaiting_verification" and jobs_named(jobs, "roadloop.stop_monitor")

    # Next rider's phone still sees the crater, hard -> reopen + escalate.
    _, jobs = wf.ingest_detection(s, det(r3, conf=0.92, jolt=0.8), None)
    assert h.state == "escalated" and h.escalation_level == 1
    child_id = jobs_named(jobs, "roadloop.file_complaint")[0]["complaint_id"]
    child = s.get(Complaint, child_id)
    assert child.kind == "escalation" and child.portal_key == "demo_escalation"
    wf.start_filing(s, child.id, tf)
    assert "DMG-1" in tf.runs["run2"] and "still detected" in tf.runs["run2"]
    wf.handle_run(s, filed("run2", "DMG-2"))
    assert child.status == "filed" and h.state == "escalated"

    # Escalation closes; this time the road really is fixed.
    wf.start_tracking(s, child.id, tf)
    wf.on_monitor_webhook(s, {"fetch_monitor_id": child.monitor_id, "results": [{"text": "Complaint No: DMG-2\nStatus: Disposed"}]})
    assert h.state == "awaiting_verification"
    with pytest.raises(wf.WorkflowError):
        wf.record_pass(s, h.id, r4, LAT + 0.01, LON)  # 1 km away does not count
    wf.record_pass(s, h.id, r4, LAT, LON)
    assert h.state == "awaiting_verification"
    wf.record_pass(s, h.id, r2, LAT, LON)
    assert h.state == "verified_fixed" and h.priority == 0

    # A new pothole at the same spot later is a recurrence, not the old ticket.
    d5, _ = wf.ingest_detection(s, det(r1), None)
    assert d5.hotspot_id != h.id and s.get(Hotspot, d5.hotspot_id).recurrence_of_id == h.id


def test_needs_human_and_retry(s):
    tf = FakeTF()
    a, b = riders(s, 2)
    wf.ingest_detection(s, det(a, dlat=0.01), None)
    d, _ = wf.ingest_detection(s, det(b, dlat=0.01), None)
    h = s.get(Hotspot, d.hotspot_id)
    c, _ = wf.approve_filing(s, h.id, a)
    wf.start_filing(s, c.id, tf)
    rid = f"run{tf.n}"
    wf.handle_run(s, {"run_id": rid, "status": "COMPLETED",
                      "result": {"status": "needs_human", "blocked_on": "OTP", "photo_attached": False}})
    assert h.state == "filing_failed" and c.status == "needs_human" and c.needs_human_reason == "OTP"
    c2, _ = wf.approve_filing(s, h.id, b)   # retry after the rider finishes OTP / login
    assert h.state == "filing" and c2.id != c.id


def test_sla_escalation_and_fetch_checks(s):
    tf = FakeTF()
    a, b = riders(s, 2)
    wf.ingest_detection(s, det(a, dlat=0.02), None)
    d, _ = wf.ingest_detection(s, det(b, dlat=0.02), None)
    h = s.get(Hotspot, d.hotspot_id)
    c, _ = wf.approve_filing(s, h.id, a)
    wf.start_filing(s, c.id, tf)
    wf.handle_run(s, filed(f"run{tf.n}", "DMG-9"))
    # No monitor (e.g. quota hit) -> picked up by batched Fetch checks.
    fetch, agent = wf.due_status_checks(s)
    assert c in fetch
    wf.check_by_fetch(s, [c], tf)
    assert h.state == "in_progress"
    c.filed_at = wf.now_utc() - timedelta(days=30)
    jobs = wf.check_sla(s)
    assert h.state == "escalated" and jobs_named(jobs, "roadloop.file_complaint")


def test_goal_failure_marks_filing_failed(s):
    tf = FakeTF()
    a, b = riders(s, 2)
    wf.ingest_detection(s, det(a, dlat=0.03), None)
    d, _ = wf.ingest_detection(s, det(b, dlat=0.03), None)
    h = s.get(Hotspot, d.hotspot_id)
    c, _ = wf.approve_filing(s, h.id, a)
    wf.start_filing(s, c.id, tf)
    wf.handle_run(s, {"run_id": f"run{tf.n}", "status": "COMPLETED",
                      "result": {"status": "failure", "reason": "Form not found"}})
    assert h.state == "filing_failed" and "Form not found" in c.needs_human_reason


# ------------------------------------------------------------------- HTTP API
def test_http_api(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    queued = []
    monkeypatch.setattr(main, "enqueue", lambda jobs: queued.extend(jobs))
    client = TestClient(main.app)
    assert client.get("/health").json()["ok"]
    assert client.post("/v1/riders", json={"display_name": "X", "consent": False}).status_code == 400
    ids = [client.post("/v1/riders", json={"display_name": f"R{i}", "phone": "9876543210", "consent": True}).json()["id"]
           for i in range(2)]

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (90, 90, 90)).save(buf, "JPEG")
    for i, rid in enumerate(ids):
        meta = {"rider_id": rid, "lat": 25.6100, "lon": 85.1400, "area_frac": 0.08, "model_conf": 0.9,
                "jolt_g": 0.7, "speed_kmh": 20, "client_id": f"c{i}", "road_name": "Bailey Road", "city_key": "patna"}
        r = client.post("/v1/detections", data={"meta": json.dumps(meta)},
                        files={"photo": ("p.jpg", buf.getvalue(), "image/jpeg")})
        assert r.status_code == 201, r.text
    hid = r.json()["hotspot_id"]
    assert r.json()["hotspot_state"] == "confirmed"
    # idempotent retry
    again = client.post("/v1/detections", data={"meta": json.dumps(meta)})
    assert again.json()["detection_id"] == r.json()["detection_id"]

    tasks = client.get(f"/v1/riders/{ids[0]}/tasks").json()
    assert tasks["approve"][0]["id"] == hid
    assert client.post(f"/v1/hotspots/{hid}/approve", json={"rider_id": ids[0]}).status_code == 200
    assert ("roadloop.file_complaint" in [n for n, _ in queued])

    q = client.get("/v1/hotspots").json()
    assert q[0]["id"] == hid and q[0]["best_photo_url"].endswith(".jpg")
    detail = client.get(f"/v1/hotspots/{hid}").json()
    assert any(e["action"] == "approved_filing" for e in detail["timeline"])
    assert client.post("/webhooks/wrong/agent", json={}).status_code == 404
    r = client.post("/v1/admin/run/check_sla").json()
    assert r["escalated"] == 0 and "SLA" in r["message"]
    assert "message" in client.post("/v1/admin/run/check_statuses").json()
    assert client.post("/v1/admin/run/nope").status_code == 404
    assert "by_state" in client.get("/v1/stats").json()

    mine = client.get(f"/v1/riders/{ids[0]}/hotspots").json()
    assert [h["id"] for h in mine] == [hid]
    assert client.get(f"/v1/riders/{ids[0]}").json()["display_name"] == "R0"
    cfg = client.get("/v1/config").json()
    assert "verify_radius_m" in cfg and "model_url" in cfg
    assert client.get("/app/").status_code == 200          # volunteer app is served


def test_manual_reports_and_geocode(s):
    a, b = riders(s, 2)
    d = det(a, dlat=0.04)
    d.road_name = d.address = d.city_key = d.state_key = None
    d.source, d.jolt_g, d.model_conf = "manual", None, 0.75
    det1, jobs = wf.ingest_detection(s, d, None)
    h = s.get(Hotspot, det1.hotspot_id)
    assert det1.source == "manual" and h.state == "candidate"
    assert jobs == []  # GEOCODER=off in tests; with nominatim this queues roadloop.geocode_hotspot
    d2 = det(b, dlat=0.04)
    d2.road_name = d2.address = d2.city_key = d2.state_key = None
    d2.source, d2.jolt_g, d2.model_conf = "manual", None, 0.75
    wf.ingest_detection(s, d2, None)
    assert h.state == "confirmed"          # two manual reporters at 0.75 -> 0.94 confidence
    assert h.authority_key is None
    wf.apply_geocode(s, h.id, {"road_name": "NH 44", "road_ref": "NH 44", "address": "NH 44, Alipur",
                               "city_key": "delhi", "state_key": "dl"})
    assert h.address == "NH 44, Alipur" and h.road_class == "national" and h.authority_key == "nhai"
