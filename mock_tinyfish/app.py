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
# Complaints persist to a JSON file (a docker volume) so the officer desk survives restarts.
DATA_FILE = os.environ.get("PORTAL_DATA", "/data/portal.json")
STATUSES = ("Pending", "Under Process", "Closed", "Escalated", "Rejected")
_CID = re.compile(r"DMG-\d{4}-\d{6}")


def _save() -> None:
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(COMPLAINTS, f)
        os.replace(tmp, DATA_FILE)
    except OSError:
        pass  # read-only or missing volume: keep working in memory


def _load() -> None:
    global _seq
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            COMPLAINTS.update(json.load(f))
    except (OSError, ValueError):
        return
    nums = [int(k.rsplit("-", 1)[1]) for k in COMPLAINTS if k.rsplit("-", 1)[-1].isdigit()]
    _seq = itertools.count(max(nums, default=1000) + 1)


def new_complaint(fields: dict, kind: str = "grievance") -> str:
    cid = f"DMG-{datetime.now().year}-{next(_seq):06d}"
    COMPLAINTS[cid] = {"id": cid, "kind": kind, "status": "Pending", "created_at": now(), "remarks": "",
                       "fields": fields, "history": [["Pending", now(), "Registered online"]]}
    _save()
    return cid


def portal_status_text(cid: str) -> str:
    c = COMPLAINTS.get(cid)
    if not c:
        return f"Complaint No: {cid}\nNo record found."
    return (f"# Demo Municipal Grievance Portal\n\nComplaint No: {cid}\nCategory: Roads > Potholes\n"
            f"Status: {c['status']}\nRemarks: {c['remarks'] or '-'}\nLast updated: {c['history'][-1][1]}\n")


CSS = """
:root{--ink:#1d2430;--muted:#5b6574;--line:#d9dee5;--bg:#f4f6f9;--brand:#0b4f8a;--warn:#fff4d6;--warnl:#e3b341}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,Segoe UI,Arial;color:var(--ink);background:var(--bg)}
.demo{background:var(--warn);border-bottom:1px solid var(--warnl);padding:6px 16px;font-size:13px;text-align:center}
header.gov{background:var(--brand);color:#fff;padding:14px 24px;display:flex;align-items:center;gap:14px}
header.gov .seal{width:40px;height:40px;border-radius:50%;border:3px solid #fff;display:grid;place-items:center;font-weight:700}
header.gov h1{font-size:19px;margin:0}header.gov p{margin:0;font-size:13px;opacity:.85}
nav{background:#083a66;padding:0 24px}nav a{color:#dce8f5;text-decoration:none;display:inline-block;padding:9px 12px;font-size:14px}
nav a.on,nav a:hover{background:#0b4f8a;color:#fff}
main{max-width:1100px;margin:22px auto;padding:0 16px}.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px}
h2{margin:0 0 12px;font-size:20px}label{display:block;font-weight:600;margin:12px 0 4px;font-size:14px}
input,select,textarea{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:6px;font:inherit;background:#fff}
button,.btn{background:var(--brand);color:#fff;border:0;border-radius:6px;padding:9px 16px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
button.light{background:#e8eef5;color:var(--brand)}.muted{color:var(--muted)}.mono{font-family:ui-monospace,Consolas,monospace}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}@media(max-width:720px){.grid2{grid-template-columns:1fr}}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:700}
.b-Pending{background:#eef1f5;color:#3d4757}.b-Under{background:#fff1d6;color:#8a5a00}.b-Closed{background:#dff3e4;color:#1d6b35}.b-Rejected{background:#fde2e1;color:#9b1c1c}.b-Escalated{background:#fde8d7;color:#9a3412}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}.kpi{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:130px}
.kpi b{display:block;font-size:24px}.kpi span{font-size:13px;color:var(--muted)}
.tabs a{display:inline-block;padding:6px 12px;border-radius:99px;border:1px solid var(--line);margin-right:6px;text-decoration:none;color:var(--ink);background:#fff;font-size:13px}
.tabs a.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.ticket{display:grid;grid-template-columns:140px 1fr 280px;gap:16px;align-items:start}@media(max-width:900px){.ticket{grid-template-columns:1fr}}
.ticket img{width:140px;height:105px;object-fit:cover;border-radius:8px;border:1px solid var(--line);background:#eee}
.noimg{width:140px;height:105px;border-radius:8px;border:1px dashed var(--line);display:grid;place-items:center;color:var(--muted);font-size:12px}
.hist{font-size:12px;color:var(--muted);margin:8px 0 0;padding-left:16px}.desc{white-space:pre-wrap;font-size:14px;margin:6px 0}
.ok{background:#dff3e4;border:1px solid #9fd3ae;padding:14px;border-radius:8px}.big{font-size:26px;font-weight:800;letter-spacing:.5px}
pre.status{background:#f7f9fb;border:1px solid var(--line);padding:14px;border-radius:8px;white-space:pre-wrap}
"""


def page(title: str, body: str, active: str = "", officer: bool = False) -> HTMLResponse:
    links = ([("Officer desk", "/portal/admin", "admin")] if officer else
             [("Register grievance", "/portal/new", "new"), ("Track status", "/portal/status", "status")])
    nav = "".join(f"<a href='{u}' class='{'on' if k == active else ''}'>{t}</a>" for t, u, k in links)
    return HTMLResponse(f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class=demo>FICTIONAL DEMO PORTAL built for RoadLoop testing. Not a government website.</div>
<header class=gov><div class=seal>DM</div><div><h1>Demo Municipal Corporation · Public Grievance Redressal</h1>
<p>{'Officer desk: roads and infrastructure' if officer else 'Roads · Streetlights · Sanitation'}</p></div></header>
<nav>{nav}</nav><main>{body}</main></body></html>""")


def _badge(status: str) -> str:
    return f"<span class='badge b-{status.split()[0]}'>{html.escape(status)}</span>"


async def portal_new(request: Request):
    escalation = request.url.path.endswith("/escalate")
    if request.method == "GET":
        title = "Escalate an unresolved grievance" if escalation else "Register a new grievance"
        subj = ("<label for=subject>Subject</label><input id=subject name=subject required>" if escalation else "")
        body = f"""<div class=card><h2>{title}</h2>
<form method=post enctype=multipart/form-data>
<label for=category>Category</label><select id=category name=category>
<option>Roads &gt; Potholes</option><option>Escalation of unresolved grievance</option><option>Streetlights</option><option>Garbage</option></select>
{subj}<div class=grid2><div><label for=name>Your name</label><input id=name name=name required></div>
<div><label for=mobile>Mobile number</label><input id=mobile name=mobile required inputmode=numeric></div></div>
<label for=address>Location / address</label><input id=address name=address required>
<label for=landmark>Landmark or GPS</label><input id=landmark name=landmark>
<label for=description>Description</label><textarea id=description name=description rows=5 required></textarea>
<label for=photo>Photo (optional)</label><input id=photo type=file name=photo accept=image/*>
<p><button type=submit>Submit grievance</button></p></form></div>"""
        return page(title, body, "new")
    form = await request.form()
    fields = {k: (v if isinstance(v, str) else f"<file {getattr(v, 'filename', '')}>") for k, v in form.items()}
    cid = new_complaint(fields, kind="escalation" if escalation else "grievance")
    if escalation:
        _link_escalation(cid)
    what = "Escalation" if escalation else "Grievance"
    return page("Submitted", f"""<div class=card><div class=ok><h2>{what} registered successfully</h2>
<p>Complaint No: <span class=big id=complaint-id>{cid}</span></p>
<p>Keep this number to track your complaint. <a href='/portal/status/{cid}'>Track {cid}</a></p></div></div>""", "new")


portal_escalate = portal_new


def _link_escalation(cid: str) -> None:
    """An escalation names the complaint it escalates: mark that one Escalated and link both ways."""
    text = " ".join(str(v) for v in COMPLAINTS[cid]["fields"].values())
    parent = next((m for m in _CID.findall(text) if m != cid and m in COMPLAINTS), None)
    if not parent:
        return
    p = COMPLAINTS[parent]
    p["status"], p["remarks"], p["escalated_to"] = "Escalated", f"Reopened: road users still report the pothole. Escalated as {cid}", cid
    p["history"].append(["Escalated", now(), f"Escalated as {cid}"])
    COMPLAINTS[cid]["escalates"] = parent
    _save()


async def portal_status_lookup(request: Request):
    cid = (request.query_params.get("id") or "").strip()
    if cid:
        return RedirectResponse(f"/portal/status/{cid}", status_code=303)
    return page("Track status", """<div class=card><h2>Track your complaint</h2><form method=get>
<label for=id>Complaint number</label><input id=id name=id placeholder="DMG-2026-001001" required>
<p><button type=submit>Check status</button></p></form></div>""", "status")


async def portal_status(request: Request):
    cid = request.path_params["cid"]
    c = COMPLAINTS.get(cid)
    hist = ""
    if c:
        hist = "<ol class=hist>" + "".join(
            f"<li>{html.escape(h[0])} · {html.escape(h[1][:16].replace('T', ' '))} UTC"
            f"{' · ' + html.escape(h[2]) if len(h) > 2 and h[2] else ''}</li>" for h in c["history"]) + "</ol>"
    head = f"<h2>Complaint {html.escape(cid)} {_badge(c['status']) if c else ''}</h2>"
    return page(f"Status {cid}", f"<div class=card>{head}<pre class=status>{html.escape(portal_status_text(cid))}</pre>"
                                 f"{hist}</div>", "status")


_URL = re.compile(r"https?://\S+?\.(?:jpe?g|png|webp)", re.I)
_GPS = re.compile(r"(-?\d{1,2}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})")


def _ticket(c: dict) -> str:
    f = c.get("fields", {})
    text = " ".join(str(v) for v in f.values())
    photo = _URL.search(text)
    gps = _GPS.search(f.get("landmark", "") + " " + text)
    img = (f"<a href='{html.escape(photo.group(0))}' target=_blank><img src='{html.escape(photo.group(0))}' alt='Complaint photo'></a>"
           if photo else "<div class=noimg>No photo</div>")
    maps = (f" · <a href='https://maps.google.com/?q={gps.group(1)},{gps.group(2)}' target=_blank>map</a>" if gps else "")
    mob = f.get("mobile", "")
    mob = (mob[:2] + "•" * max(0, len(mob) - 4) + mob[-2:]) if len(mob) > 4 else mob
    hist = "".join(f"<li>{html.escape(h[0])} · {html.escape(h[1][11:16])} UTC"
                   f"{' · ' + html.escape(h[2]) if len(h) > 2 and h[2] else ''}</li>" for h in c["history"])
    opts = "".join(f"<option {'selected' if s == c['status'] else ''}>{s}</option>" for s in STATUSES)
    kind = "Escalation" if c.get("kind") == "escalation" else "Grievance"
    if c.get("escalates"):
        kind += f" of <a href='#{c['escalates']}'>{c['escalates']}</a>"
    if c.get("escalated_to"):
        kind += f" · escalated as <a href='#{c['escalated_to']}'>{c['escalated_to']}</a>"
    return f"""<div class='card ticket' id='{c['id']}'>{img}
<div><div><b class=mono>{c['id']}</b> {_badge(c['status'])} <span class=muted>· {kind} · {html.escape(c['created_at'][:16].replace('T', ' '))} UTC</span></div>
<div><b>{html.escape(f.get('subject') or f.get('category') or 'Roads > Potholes')}</b></div>
<div class=muted>{html.escape(f.get('address', ''))}{maps}</div>
<div class=desc>{html.escape(f.get('description', '')[:600])}</div>
<div class=muted>Citizen: {html.escape(f.get('name', '-'))} · {html.escape(mob)}</div><ol class=hist>{hist}</ol></div>
<form method=post action='/portal/admin/{c['id']}'><label>Update status</label><select name=status>{opts}</select>
<label>Officer remarks</label><input name=remarks placeholder="e.g. Patch work completed">
<p><button type=submit>Update</button></p></form></div>"""


async def portal_admin(request: Request):
    want = request.query_params.get("status", "")
    rows = sorted(COMPLAINTS.values(), key=lambda c: c["created_at"], reverse=True)
    counts = {s: sum(1 for c in rows if c["status"] == s) for s in STATUSES}
    shown = [c for c in rows if not want or c["status"] == want]
    kpis = "".join(f"<div class=kpi><b>{counts[s]}</b><span>{s}</span></div>" for s in STATUSES)
    tabs = "<div class=tabs>" + "".join(
        f"<a href='/portal/admin{('?status=' + s) if s else ''}' class='{'on' if want == s else ''}'>{s or 'All'}</a>"
        for s in ("",) + STATUSES) + "</div>"
    tickets = "".join(_ticket(c) for c in shown) or "<div class=card muted>No complaints yet. They appear here as soon as RoadLoop files one.</div>"
    body = (f"<div class=kpis>{kpis}</div><div class=card style='display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap'>"
            f"{tabs}<span class=muted>Closing a ticket here is what RoadLoop independently re-checks on the road.</span></div>{tickets}"
            "<script>setInterval(()=>{if(!document.activeElement||!['INPUT','SELECT'].includes(document.activeElement.tagName))location.reload()},10000)</script>")
    return page("Officer desk", body, "admin", officer=True)


async def _notify_roadloop():
    """Live mode: ask RoadLoop to re-read portal statuses now (via TinyFish Fetch) instead of waiting for
    the scheduled Monitor. Mock mode: simulate the Monitor firing."""
    if os.environ.get("TINYFISH_MODE", "mock") != "live":
        await _tick_monitors()
        return
    api = os.environ.get("ROADLOOP_API", "http://api:8000")
    headers = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]} if os.environ.get("ADMIN_TOKEN") else {}
    try:
        await asyncio.to_thread(requests.post, f"{api}/v1/admin/run/check_statuses", headers=headers, timeout=60)
    except requests.RequestException:
        pass


async def portal_admin_set(request: Request):
    cid = request.path_params["cid"]
    form = await request.form()
    status = str(form.get("status", "Closed"))
    if status not in STATUSES:
        status = "Closed"
    if cid in COMPLAINTS:
        c = COMPLAINTS[cid]
        remark = str(form.get("remarks", "")).strip()[:200] or ("Work completed" if status == "Closed" else "")
        c["status"], c["remarks"] = status, remark
        c["history"].append([status, now(), remark])
        _save()
        if os.environ.get("MOCK_AUTO_TICK", "1") == "1":
            asyncio.create_task(_notify_roadloop())
    return RedirectResponse(f"/portal/admin#{cid}", status_code=303)


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


_load()

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
    Route("/portal/status", portal_status_lookup),
    Route("/portal/status/{cid}", portal_status),
    Route("/portal/admin", portal_admin),
    Route("/portal/admin/{cid}", portal_admin_set, methods=["POST"]),
])