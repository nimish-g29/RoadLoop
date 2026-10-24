"""Mock TinyFish + a fictional municipal grievance portal, for offline demos and tests.

Two jobs:
 1. Speak the same REST shapes as TinyFish (Agent runs, Search, Fetch, Monitors,
    webhooks) so the backend can run end to end without an API key or credits.
 2. Serve a clearly fictional "Demo Municipal Grievance Portal" (/portal/...).
    Exposed through a tunnel, the REAL TinyFish agent can file on it too, which
    gives a CAPTCHA-free live demo. /portal/admin lets you play the officer who
    closes tickets, so judges can watch RoadLoop catch a fake closure.

Run: uvicorn app:app --port 8100
"""
from __future__ import annotations

import asyncio
import html
import itertools
import json
import os
import re
import uuid
from datetime import datetime, timezone

import requests
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

RUN_SECONDS = float(os.environ.get("MOCK_RUN_SECONDS", "2"))
API_KEY_REQUIRED = os.environ.get("MOCK_REQUIRE_KEY", "1") == "1"

RUNS: dict[str, dict] = {}
MONITORS: dict[str, dict] = {}
COMPLAINTS: dict[str, dict] = {}
_seq = itertools.count(1001)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _auth(request: Request) -> JSONResponse | None:
    if API_KEY_REQUIRED and not request.headers.get("x-api-key"):
        return JSONResponse({"error": {"code": "MISSING_API_KEY", "message": "X-API-Key header required"}}, 401)
    return None


# ------------------------------------------------------------- fake portal
def new_complaint(fields: dict, kind: str = "grievance") -> str:
    cid = f"DMG-{datetime.now().year}-{next(_seq):06d}"
    COMPLAINTS[cid] = {"id": cid, "kind": kind, "status": "Pending", "created_at": now(), "remarks": "",
                       "fields": fields, "history": [("Pending", now())]}
    return cid


def portal_status_text(cid: str) -> str:
    c = COMPLAINTS.get(cid)
    if not c:
        return f"Complaint No: {cid}\nNo record found."
    return (f"# Demo Municipal Grievance Portal\n\nComplaint No: {cid}\nCategory: Roads > Potholes\n"
            f"Status: {c['status']}\nRemarks: {c['remarks'] or '-'}\nLast updated: {c['history'][-1][1]}\n")


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>{title}</title>
<style>body{{font-family:system-ui;max-width:720px;margin:2rem auto;padding:0 1rem}}
label{{display:block;margin:.6rem 0 .2rem}}input,textarea,select{{width:100%;padding:.5rem}}
.banner{{background:#fff3cd;padding:.5rem;border:1px solid #e0c060}}button{{margin-top:1rem;padding:.6rem 1.2rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:.4rem}}</style></head>
<body><p class=banner>FICTIONAL DEMO PORTAL for RoadLoop testing. Not a government website.</p>{body}</body></html>"""


async def portal_new(request: Request):
    if request.method == "GET":
        body = """<h1>Register a New Grievance</h1>
<form method=post enctype=multipart/form-data>
<label>Category</label><select name=category><option>Roads &gt; Potholes</option><option>Streetlights</option><option>Garbage</option></select>
<label>Your name</label><input name=name required>
<label>Mobile number</label><input name=mobile required>
<label>Location / address</label><input name=address required>
<label>Landmark or GPS</label><input name=landmark>
<label>Description</label><textarea name=description rows=4 required></textarea>
<label>Photo (optional)</label><input type=file name=photo accept=image/*>
<button type=submit>Submit grievance</button></form>"""
        return HTMLResponse(PAGE.format(title="New Grievance", body=body))
    form = await request.form()
    fields = {k: (v if isinstance(v, str) else f"<file {getattr(v, 'filename', '')}>") for k, v in form.items()}
    cid = new_complaint(fields)
    return HTMLResponse(PAGE.format(title="Submitted", body=(
        f"<h1>Grievance registered</h1><p>Your complaint number is <b id=complaint-id>{cid}</b>. "
        f"Track it at <a href='/portal/status/{cid}'>/portal/status/{cid}</a>.</p>")))


async def portal_escalate(request: Request):
    if request.method == "GET":
        return await portal_new(request)
    form = await request.form()
    cid = new_complaint({k: str(v) for k, v in form.items()}, kind="escalation")
    return HTMLResponse(PAGE.format(title="Escalated", body=f"<h1>Escalation registered</h1><p>Reference <b>{cid}</b>.</p>"))


async def portal_status(request: Request):
    cid = request.path_params["cid"]
    text = portal_status_text(cid)
    return HTMLResponse(PAGE.format(title=f"Status {cid}", body="<pre>" + html.escape(text) + "</pre>"))


async def portal_admin(request: Request):
    rows = "".join(
        f"<tr><td>{c['id']}</td><td>{html.escape(c['fields'].get('address', ''))}</td><td>{c['status']}</td><td>"
        + "".join(f"<form method=post action='/portal/admin/{c['id']}' style='display:inline'>"
                  f"<input type=hidden name=status value='{s}'><button>{s}</button></form> "
                  for s in ("Under Process", "Closed", "Pending"))
        + "</td></tr>" for c in COMPLAINTS.values())
    body = (f"<h1>Officer console (demo)</h1><table><tr><th>ID</th><th>Address</th><th>Status</th><th>Set</th></tr>"
            f"{rows}</table><p>Closing a ticket here then <a href='/mock/monitors/tick'>ticking monitors</a> "
            f"simulates a scheduled TinyFish Monitor run.</p>")
    return HTMLResponse(PAGE.format(title="Officer console", body=body))


async def portal_admin_set(request: Request):
    cid = request.path_params["cid"]
    form = await request.form()
    status = str(form.get("status", "Closed"))
    if cid in COMPLAINTS:
        COMPLAINTS[cid]["status"] = status
        COMPLAINTS[cid]["remarks"] = "Work completed" if status == "Closed" else ""
        COMPLAINTS[cid]["history"].append((status, now()))
        if os.environ.get("MOCK_AUTO_TICK", "1") == "1":
            asyncio.create_task(_tick_monitors())
    return RedirectResponse("/portal/admin", status_code=303)


# ------------------------------------------------------------ agent runs
def _simulate(url: str, goal: str) -> dict:
    """Decide what a 'perfect agent' would return for this goal (intent from the goal text first)."""
    m = re.search(r"complaint number (\S+)", goal)
    if "Check the status" in goal and m:
        cid = m.group(1).rstrip(".")
        c = COMPLAINTS.get(cid)
        if not c:
            return {"found": False}
        return {"found": True, "status_label": c["status"], "remarks": c["remarks"], "last_updated": c["history"][-1][1]}
    if "/portal/new" in url or "/portal/escalate" in url:
        if "OTP_REQUIRED" in goal:
            return {"status": "needs_human", "complaint_id": None, "photo_attached": False, "blocked_on": "OTP"}
        addr = re.search(r"Location / address: (.+)", goal)
        cid = new_complaint({"address": addr.group(1).strip() if addr else "", "description": goal[:400]},
                            kind="escalation" if "escalat" in url else "grievance")
        return {"status": "filed", "complaint_id": cid, "photo_attached": "upload the image" in goal,
                "portal_message": f"Your complaint number is {cid}", "blocked_on": None}
    if "responsible for maintaining" in goal:
        return {"authority": "municipal_corporation", "authority_name": "Demo Municipal Corporation",
                "complaint_url": "/portal/new", "escalation_contact": "Zonal Commissioner", "evidence_url": None}
    return {"status": "failure", "reason": "mock agent does not understand this goal"}


def _new_run(body: dict) -> dict:
    rid = f"run_{uuid.uuid4().hex[:12]}"
    run = {"run_id": rid, "status": "PENDING", "goal": body.get("goal"), "created_at": now(), "started_at": None,
           "finished_at": None, "num_of_steps": None, "result": None, "error": None,
           "streaming_url": f"http://mock/stream/{rid}", "output_schema": body.get("output_schema"), "steps": [],
           "_url": body.get("url"), "_webhook": body.get("webhook_url")}
    RUNS[rid] = run
    return run


def _public(run: dict) -> dict:
    return {k: v for k, v in run.items() if not k.startswith("_")}


def _post(url: str, payload: dict):
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        print("webhook delivery failed", url, e)


async def _execute(run: dict):
    run["status"], run["started_at"] = "RUNNING", now()
    await asyncio.sleep(RUN_SECONDS)
    if run["status"] == "CANCELLED":
        return
    result = _simulate(run["_url"] or "", run["goal"] or "")
    run.update(status="COMPLETED", finished_at=now(), num_of_steps=7, result=result,
               steps=[{"purpose": "Opened page"}, {"purpose": "Filled form"}, {"purpose": "Submitted"}])
    if run["_webhook"]:
        await asyncio.to_thread(_post, run["_webhook"], {"event": "run.completed", "run_id": run["run_id"],
                                                         "status": "COMPLETED", "data": _public(run)})


async def run_async(request: Request):
    if (e := _auth(request)):
        return e
    body = await request.json()
    if not body.get("url") or not body.get("goal"):
        return JSONResponse({"run_id": None, "error": {"code": "INVALID_INPUT", "message": "url and goal required"}}, 400)
    run = _new_run(body)
    asyncio.create_task(_execute(run))
    return JSONResponse({"run_id": run["run_id"], "error": None})


async def run_sync(request: Request):
    if (e := _auth(request)):
        return e
    run = _new_run(await request.json())
    run["_webhook"] = None
    await _execute(run)
    p = _public(run)
    return JSONResponse({k: p[k] for k in ("run_id", "status", "started_at", "finished_at", "num_of_steps", "result", "error")})


async def run_sse(request: Request):
    if (e := _auth(request)):
        return e
    run = _new_run(await request.json())
    run["_webhook"] = None

    async def gen():
        yield {"data": json.dumps({"type": "STARTED", "run_id": run["run_id"], "timestamp": now()})}
        yield {"data": json.dumps({"type": "STREAMING_URL", "run_id": run["run_id"],
                                   "streaming_url": run["streaming_url"], "timestamp": now()})}
        for p in ("Opening the grievance form", "Filling location and description", "Submitting"):
            await asyncio.sleep(RUN_SECONDS / 3)
            yield {"data": json.dumps({"type": "PROGRESS", "run_id": run["run_id"], "purpose": p, "timestamp": now()})}
        result = _simulate(run["_url"] or "", run["goal"] or "")
        run.update(status="COMPLETED", finished_at=now(), result=result)
        yield {"data": json.dumps({"type": "COMPLETE", "run_id": run["run_id"], "status": "COMPLETED",
                                   "result": result, "timestamp": now()})}
    return EventSourceResponse(gen())


async def get_run(request: Request):
    if (e := _auth(request)):
        return e
    run = RUNS.get(request.path_params["rid"])
    if not run:
        return JSONResponse({"error": {"code": "NOT_FOUND", "message": "run not found"}}, 404)
    return JSONResponse(_public(run))


async def cancel_run(request: Request):
    run = RUNS.get(request.path_params["rid"])
    if not run:
        return JSONResponse({"error": {"code": "NOT_FOUND", "message": "run not found"}}, 404)
    if run["status"] in ("PENDING", "RUNNING"):
        run["status"] = "CANCELLED"
    return JSONResponse({"run_id": run["run_id"], "status": run["status"], "cancelled_at": now(), "message": None})


# --------------------------------------------------------- search / fetch
async def search(request: Request):
    if (e := _auth(request)):
        return e
    q = request.query_params.get("query", "")
    results = [{"position": 1, "site_name": "demo.gov.example", "title": f"Road maintenance responsibility - {q[:40]}",
                "snippet": "Municipal roads are maintained by the Demo Municipal Corporation; national highways by NHAI.",
                "url": "http://mock/portal/new"}]
    return JSONResponse({"query": q, "results": results, "total_results": 1, "page": 1})


def _fetch_one(url: str) -> dict | None:
    m = re.search(r"/portal/status/([\w-]+)", url)
    if m:
        return {"url": url, "final_url": url, "title": f"Status {m.group(1)}", "description": None,
                "language": "en", "format": "markdown", "text": portal_status_text(m.group(1))}
    return None


async def fetch(request: Request):
    if (e := _auth(request)):
        return e
    body = await request.json()
    urls = body.get("urls") or []
    if not 1 <= len(urls) <= 10:
        return JSONResponse({"error": {"code": "INVALID_INPUT", "message": "1-10 urls"}}, 400)
    results, errors = [], []
    for u in urls:
        r = _fetch_one(u)
        (results.append(r) if r else errors.append({"url": u, "error": "mock can only fetch demo portal pages"}))
    return JSONResponse({"results": results, "errors": errors})


# ---------------------------------------------------------------- monitors
def _monitor_run(m: dict, baseline: bool = False) -> dict:
    run = {"id": str(uuid.uuid4()), "is_baseline": baseline, "created_at": now()}
    if m["type"] == "fetch":
        r = _fetch_one(m["config"]["url"])
        run.update(fetch_monitor_id=m["id"], results=[r] if r else [],
                   errors=[] if r else [{"url": m["config"]["url"], "error": "unreachable"}])
    else:
        run.update(search_monitor_id=m["id"], query=m["config"]["query"], status_code=200, result_count=0,
                   results=[], new_result_positions=[])
    return run


async def monitors(request: Request):
    if (e := _auth(request)):
        return e
    if request.method == "GET":
        return JSONResponse({"monitors": list(MONITORS.values())})
    body = await request.json()
    if body.get("type") not in ("fetch", "search") or not body.get("schedule_cron"):
        return JSONResponse({"error": {"code": "INVALID_INPUT", "message": "type and schedule_cron required"}}, 400)
    if len(MONITORS) >= 50:
        return JSONResponse({"error": {"code": "FORBIDDEN", "message": "monitor limit reached (50)"}}, 403)
    mid = str(uuid.uuid4())
    m = {"id": mid, "name": body.get("name"), "type": body["type"], "config": body.get("config", {}),
         "schedule_cron": body["schedule_cron"], "status": "active", "last_error": None,
         "webhook_url": body.get("webhook_url"), "created_at": now(), "updated_at": now()}
    MONITORS[mid] = m
    return JSONResponse({"monitor": m, "run": _monitor_run(m, baseline=True)}, 201)


async def monitor_item(request: Request):
    if (e := _auth(request)):
        return e
    m = MONITORS.get(request.path_params["mid"])
    if not m:
        return JSONResponse({"error": {"code": "NOT_FOUND", "message": "monitor not found"}}, 404)
    if request.method == "GET":
        return JSONResponse({"monitor": m})
    if request.method == "DELETE":
        MONITORS.pop(m["id"])
        return JSONResponse({"deleted": True, "id": m["id"]})
    body = await request.json()
    if "status" in body and len(body) > 1:
        return JSONResponse({"error": {"code": "INVALID_INPUT", "message": "status cannot be combined"}}, 400)
    m.update({k: v for k, v in body.items() if k in ("status", "name", "schedule_cron", "webhook_url")},
             updated_at=now())
    return JSONResponse({"monitor": m})


async def monitor_run_now(request: Request):
    m = MONITORS.get(request.path_params["mid"])
    if not m:
        return JSONResponse({"error": {"code": "NOT_FOUND", "message": "monitor not found"}}, 404)
    return JSONResponse({"run": _monitor_run(m)}, 201)  # run-now: no webhook, like the real API


async def _tick_monitors():
    for m in list(MONITORS.values()):
        if m["status"] == "active" and m.get("webhook_url"):
            await asyncio.to_thread(_post, m["webhook_url"], _monitor_run(m))


async def tick(request: Request):
    """Simulate one scheduled run of every active monitor (webhooks fire)."""
    await _tick_monitors()
    return JSONResponse({"ticked": len(MONITORS)})


async def health(request: Request):
    return JSONResponse({"ok": True, "runs": len(RUNS), "monitors": len(MONITORS), "complaints": len(COMPLAINTS)})


app = Starlette(routes=[
    Route("/health", health),
    Route("/v1/automation/run", run_sync, methods=["POST"]),
    Route("/v1/automation/run-async", run_async, methods=["POST"]),
    Route("/v1/automation/run-sse", run_sse, methods=["POST"]),
    Route("/v1/runs/{rid}", get_run),
    Route("/v1/runs/{rid}/cancel", cancel_run, methods=["POST"]),
    Route("/search", search),
    Route("/fetch", fetch, methods=["POST"]),
    Route("/v1/monitors", monitors, methods=["GET", "POST"]),
    Route("/v1/monitors/{mid}", monitor_item, methods=["GET", "PATCH", "DELETE"]),
    Route("/v1/monitors/{mid}/runs", monitor_run_now, methods=["POST"]),
    Route("/mock/monitors/tick", tick, methods=["GET", "POST"]),
    Route("/portal", lambda r: RedirectResponse("/portal/new")),
    Route("/portal/new", portal_new, methods=["GET", "POST"]),
    Route("/portal/escalate", portal_escalate, methods=["GET", "POST"]),
    Route("/portal/status/{cid}", portal_status),
    Route("/portal/admin", portal_admin),
    Route("/portal/admin/{cid}", portal_admin_set, methods=["POST"]),
])
