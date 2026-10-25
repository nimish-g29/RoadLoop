"""RoadLoop API.

Rider app:   POST /v1/riders, POST /v1/detections, POST /v1/hotspots/{id}/passes, GET /v1/riders/{id}/tasks,
             POST /v1/hotspots/{id}/approve
Dashboard:   GET /v1/hotspots (ranked queue), GET /v1/hotspots/{id}, GET /v1/stats
TinyFish:    POST /webhooks/{token}/agent, POST /webhooks/{token}/monitor
Engineers:   GET /v1/areas (area memory), POST /v1/hotspots/{id}/authority, GET /v1/authorities
Demo/admin:  POST /v1/admin/run/{job}, GET /v1/admin/tinyfish-check
Portal:      /portal/new|escalate|status/* proxied to the fictional demo portal, so the REAL TinyFish
             browser reaches it through the same https tunnel as the API (officer console stays local)
"""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import schemas
from .config import settings
from .core import geo
from .core.lifecycle import State
from .db import get_session, init_db, is_postgres
from .models import AreaAuthority, AreaPlace, AuditEvent, Complaint, Detection, Hotspot, Rider
from .services import workflow as wf

MAX_PHOTO_BYTES = 4 * 1024 * 1024
media_root = Path(settings.media_dir)
media_root.mkdir(parents=True, exist_ok=True)

@asynccontextmanager
async def lifespan(_app):
    _startup()
    yield


app = FastAPI(title="RoadLoop API", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/media", StaticFiles(directory=str(media_root)), name="media")

# Volunteer app (mobile web app). Same origin as the API, so one https tunnel serves both.
RIDER_DIR = Path(__file__).resolve().parent / "static" / "rider"
app.mount("/app", StaticFiles(directory=str(RIDER_DIR), html=True), name="rider")


@app.get("/", include_in_schema=False)
def _root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/app/")


def _startup():
    import logging
    init_db()
    log = logging.getLogger("uvicorn.error")
    if settings.tinyfish_mode == "live" and settings.tinyfish_api_key == "mock-key":
        log.error("TINYFISH_MODE=live but TINYFISH_API_KEY is empty")
    if settings.tinyfish_mode == "live" and not settings.webhooks_enabled:
        log.warning("PUBLIC_BASE_URL is not https: TinyFish webhooks/monitors disabled, falling back to polling")


def enqueue(jobs):
    if jobs:
        from .worker import enqueue as _enqueue
        _enqueue(jobs)


def _bad(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------- serialise
def hotspot_out(s: Session, h: Hotspot) -> schemas.HotspotOut:
    out = schemas.HotspotOut.model_validate(h)
    out.best_photo_url = settings.media_url(h.best_photo) if h.best_photo else None
    latest = s.scalar(select(Complaint).where(Complaint.hotspot_id == h.id).order_by(Complaint.created_at.desc()))
    out.complaint = schemas.ComplaintOut.model_validate(latest) if latest else None
    return out


# -------------------------------------------------------------------- rider
@app.get("/health")
def health(s: Session = Depends(get_session)):
    from sqlalchemy import text
    s.execute(text("SELECT 1"))
    return {"ok": True, "tinyfish_mode": settings.tinyfish_mode, "webhooks": settings.webhooks_enabled,
            "portal_base": settings.portal_base, "public_base_url": settings.public_base_url}


def require_admin(x_admin_token: str | None = Header(default=None)):
    """Admin actions need X-Admin-Token when ADMIN_TOKEN is set (it is open in local demos)."""
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(401, "admin token required")


@app.post("/v1/riders", response_model=schemas.RiderOut, status_code=201)
def create_rider(body: schemas.RiderIn, s: Session = Depends(get_session)):
    if not body.consent:
        raise HTTPException(400, "consent is required")
    r = Rider(display_name=body.display_name, phone=body.phone)
    s.add(r)
    s.commit()
    return r


def _save_photo(upload: UploadFile) -> str:
    data = upload.file.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "photo too large")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        raise HTTPException(400, "photo is not a valid image")
    img.thumbnail((1280, 1280))
    rel = f"{datetime.now(timezone.utc):%Y/%m}/{uuid.uuid4().hex}.jpg"
    path = media_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=82)  # re-encoding drops EXIF (device model, exact time, etc.)
    return rel


@app.post("/v1/detections", response_model=schemas.DetectionOut, status_code=201)
def post_detection(meta: str = Form(..., description="DetectionMeta as JSON"),
                   photo: UploadFile | None = File(None), s: Session = Depends(get_session)):
    try:
        m = schemas.DetectionMeta.model_validate(json.loads(meta))
    except (ValueError, ValidationError) as e:
        _bad(e)
    rel = _save_photo(photo) if photo else None
    try:
        det, jobs = wf.ingest_detection(s, wf.DetectionIn(**m.model_dump()), rel)
    except wf.WorkflowError as e:
        _bad(e)
    s.commit()
    enqueue(jobs)
    h = s.get(Hotspot, det.hotspot_id)
    return schemas.DetectionOut(detection_id=det.id, hotspot_id=h.id, hotspot_state=h.state, severity=det.severity)


@app.post("/v1/hotspots/{hotspot_id}/passes")
def post_pass(hotspot_id: str, body: schemas.PassIn, s: Session = Depends(get_session)):
    try:
        jobs = wf.record_pass(s, hotspot_id, body.rider_id, body.lat, body.lon)
    except wf.WorkflowError as e:
        _bad(e)
    s.commit()
    enqueue(jobs)
    return {"hotspot_state": s.get(Hotspot, hotspot_id).state}


@app.get("/v1/riders/{rider_id}/tasks")
def rider_tasks(rider_id: str, lat: float | None = None, lon: float | None = None, radius_m: float = 3000,
                s: Session = Depends(get_session)):
    """What the app should show: filings awaiting this rider's approval, and nearby spots to re-check."""
    mine = select(Detection.hotspot_id).where(Detection.rider_id == rider_id).distinct()
    approvals = s.scalars(select(Hotspot).where(Hotspot.id.in_(mine),
                                                Hotspot.state.in_((State.CONFIRMED.value,
                                                                   State.FILING_FAILED.value)))).all()
    verify = []
    if lat is not None and lon is not None:
        q = select(Hotspot).where(Hotspot.state == State.AWAITING_VERIFICATION.value)
        if is_postgres():
            from sqlalchemy import text
            q = q.where(text("ST_DWithin(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :r)")
                        .bindparams(lat=lat, lon=lon, r=radius_m))
        verify = [h for h in s.scalars(q) if geo.haversine_m(lat, lon, h.lat, h.lon) <= radius_m]
    return {
        "approve": [hotspot_out(s, h) for h in approvals],
        "verify": [{"hotspot_id": h.id, "lat": h.lat, "lon": h.lon, "radius_m": settings.verify_radius_m}
                   for h in verify],
    }


@app.get("/v1/config")
def app_config():
    """Settings the volunteer app needs. model_url is null until a trained model is dropped in."""
    model = RIDER_DIR / "model" / "pothole.onnx"
    meta = RIDER_DIR / "model" / "model.json"
    return {
        "model_url": "/app/model/pothole.onnx" if model.exists() else None,
        "model_meta": json.loads(meta.read_text()) if meta.exists() else {"imgsz": 640, "conf": 0.35},
        "verify_radius_m": settings.verify_radius_m,
        "min_riders": settings.min_riders,
        "bump": {"minG": settings.bump_min_g, "noiseK": settings.bump_noise_k,
                 "minSpeedKmh": settings.bump_min_speed_kmh},
    }


@app.get("/v1/riders/{rider_id}", response_model=schemas.RiderOut)
def get_rider(rider_id: str, s: Session = Depends(get_session)):
    r = s.get(Rider, rider_id)
    if not r:
        raise HTTPException(404, "rider not found")
    return r


@app.get("/v1/riders/{rider_id}/hotspots", response_model=list[schemas.HotspotOut])
def rider_hotspots(rider_id: str, s: Session = Depends(get_session)):
    """Everything this volunteer has reported, newest first, with live complaint status."""
    mine = select(Detection.hotspot_id).where(Detection.rider_id == rider_id).distinct()
    hs = s.scalars(select(Hotspot).where(Hotspot.id.in_(mine)).order_by(Hotspot.last_seen.desc()).limit(100))
    return [hotspot_out(s, h) for h in hs]


@app.post("/v1/hotspots/{hotspot_id}/approve", response_model=schemas.ComplaintOut)
def approve(hotspot_id: str, body: schemas.ApproveIn, s: Session = Depends(get_session)):
    try:
        c, jobs = wf.approve_filing(s, hotspot_id, body.rider_id)
    except wf.WorkflowError as e:
        _bad(e)
    s.commit()
    enqueue(jobs)
    return c


# ---------------------------------------------------------------- dashboard
@app.get("/v1/hotspots", response_model=list[schemas.HotspotOut])
def queue(state: list[str] | None = Query(None), severity: str | None = None,
          bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat"),
          include_fixed: bool = False, limit: int = Query(200, le=1000), s: Session = Depends(get_session)):
    q = select(Hotspot)
    if state:
        q = q.where(Hotspot.state.in_(state))
    elif not include_fixed:
        q = q.where(Hotspot.state != State.VERIFIED_FIXED.value)
    if severity:
        q = q.where(Hotspot.severity == severity)
    if bbox:
        try:
            w, so, e, n = (float(x) for x in bbox.split(","))
        except ValueError:
            raise HTTPException(400, "bbox must be minLon,minLat,maxLon,maxLat")
        q = q.where(Hotspot.lon.between(w, e), Hotspot.lat.between(so, n))
    q = q.order_by(Hotspot.priority.desc()).limit(limit)
    return [hotspot_out(s, h) for h in s.scalars(q)]


@app.get("/v1/hotspots/{hotspot_id}", response_model=schemas.HotspotDetail)
def hotspot_detail(hotspot_id: str, s: Session = Depends(get_session)):
    h = s.get(Hotspot, hotspot_id)
    if not h:
        raise HTTPException(404, "not found")
    base = hotspot_out(s, h).model_dump()
    return schemas.HotspotDetail(
        **base,
        complaints=[schemas.ComplaintOut.model_validate(c) for c in h.complaints],
        timeline=[schemas.AuditOut.model_validate(a) for a in s.scalars(
            select(AuditEvent).where(AuditEvent.hotspot_id == h.id).order_by(AuditEvent.id))],
        photos=[settings.media_url(d.photo) for d in h.detections if d.photo][-6:],
    )


@app.get("/v1/stats")
def stats(s: Session = Depends(get_session)):
    by_state = dict(s.execute(select(Hotspot.state, func.count()).group_by(Hotspot.state)).all())
    fixed = s.scalars(select(Hotspot).where(Hotspot.fixed_at.is_not(None))).all()
    by_authority: dict[str, list[float]] = {}
    for h in fixed:
        first = min((wf.aware(c.filed_at) for c in h.complaints if c.filed_at), default=None)
        if first:
            by_authority.setdefault(h.authority_key or "unknown", []).append(
                (wf.aware(h.fixed_at) - first).total_seconds() / 86400)
    repair_days = {k: round(sorted(v)[len(v) // 2], 1) for k, v in by_authority.items()}
    fake_closures = s.scalar(select(func.count()).select_from(AuditEvent)
                             .where(AuditEvent.action.like("state:awaiting_verification->reopened"))) or 0
    areas = s.scalar(select(func.count()).select_from(AreaAuthority)) or 0
    reuses = s.scalar(select(func.coalesce(func.sum(AreaAuthority.hits), 0))) or 0
    geo_reuses = s.scalar(select(func.coalesce(func.sum(AreaPlace.hits), 0))) or 0
    return {"by_state": by_state, "median_days_to_verified_fix": repair_days,
            "closures_caught_unfixed": fake_closures, "tinyfish_mode": settings.tinyfish_mode,
            "area_memory": {"areas": areas, "authority_reuses": int(reuses), "geocode_reuses": int(geo_reuses)},
            "open_high": s.scalar(select(func.count()).select_from(Hotspot).where(
                Hotspot.severity == "high", Hotspot.state != State.VERIFIED_FIXED.value)) or 0}


# ----------------------------------------------------------------- webhooks
def _check_token(token: str):
    if token != settings.webhook_token:
        raise HTTPException(404, "not found")


@app.post("/webhooks/{token}/agent")
async def agent_webhook(token: str, request: Request, s: Session = Depends(get_session)):
    _check_token(token)
    payload = await request.json()
    run = payload.get("data") or payload  # docs: {event, run_id, status, data: <run object>}
    run.setdefault("run_id", payload.get("run_id"))
    run.setdefault("status", payload.get("status"))
    jobs = wf.handle_run(s, run)
    s.commit()
    enqueue(jobs)
    return {"ok": True}


@app.post("/webhooks/{token}/monitor")
async def monitor_webhook(token: str, request: Request, s: Session = Depends(get_session)):
    _check_token(token)
    jobs = wf.on_monitor_webhook(s, await request.json())
    s.commit()
    enqueue(jobs)
    return {"ok": True}  # respond fast: TinyFish gives each delivery 10 s


# --------------------------------------------------------------------- demo
@app.post("/v1/admin/run/{job}", dependencies=[Depends(require_admin)])
def run_job_now(job: str, s: Session = Depends(get_session)):
    """Dashboard buttons. Runs now and reports what happened (the scheduled versions run in Celery beat).
    check_statuses: re-read every open complaint's portal status now.
    check_sla: escalate complaints open longer than SLA_DAYS."""
    from .tinyfish.client import TinyFishError, from_settings

    if job == "check_statuses":
        fetch, lookup = wf.due_status_checks(s, force=True)
        before = {c.id: c.portal_status for c in fetch}
        try:
            jobs = wf.check_by_fetch(s, fetch, from_settings(settings)) if fetch else []
            for c in lookup:
                jobs += wf.start_status_lookup(s, c, from_settings(settings))
        except TinyFishError as e:
            raise HTTPException(502, f"TinyFish error: {e}")
        changed = sum(1 for c in fetch if c.portal_status != before[c.id])
        s.commit()
        enqueue(jobs)
        n = len(fetch) + len(lookup)
        msg = ("No open complaints to check." if n == 0 else
               f"Checked {n} complaint{'s' if n != 1 else ''}: {changed} status change{'s' if changed != 1 else ''}"
               + (f", {len(lookup)} agent lookup{'s' if len(lookup) != 1 else ''} started" if lookup else "") + ".")
        return {"job": job, "checked": n, "changed": changed, "message": msg}
    if job == "check_sla":
        jobs = wf.check_sla(s)
        escalated = sum(1 for name, _ in jobs if name == "roadloop.file_complaint")
        s.commit()
        enqueue(jobs)
        msg = (f"Escalated {escalated} complaint{'s' if escalated != 1 else ''} past the {settings.sla_days}-day SLA."
               if escalated else f"No complaints past the {settings.sla_days}-day SLA.")
        return {"job": job, "escalated": escalated, "message": msg}
    if job == "refresh_priorities":
        wf.refresh_priorities(s)
        s.commit()
        return {"job": job, "message": "Priorities refreshed."}
    raise HTTPException(404, "unknown job")


# ---------------------------------------------------------- area memory
@app.get("/v1/authorities")
def authorities():
    """Authorities an engineer can assign (portal registry keys)."""
    return [{"key": k, "name": p.name, "verified": p.verified, "usable": p.usable}
            for k, p in wf.portals().items()]


@app.get("/v1/areas")
def areas(limit: int = Query(200, le=1000), s: Session = Depends(get_session)):
    """What RoadLoop has learned about who maintains which road, and how often it reused that."""
    rows = s.scalars(select(AreaAuthority).order_by(AreaAuthority.hits.desc(), AreaAuthority.updated_at.desc())
                     .limit(limit))
    out = []
    for r in rows:
        lat, lon = geo.decode(r.cell)
        out.append({"id": r.id, "cell": r.cell, "lat": lat, "lon": lon, "road": r.road_name, "road_key": r.road_key,
                    "city": r.city_key, "authority": r.authority_key, "confidence": r.confidence,
                    "source": r.source, "hits": r.hits, "evidence_url": r.evidence_url,
                    "updated_at": r.updated_at})
    return out


@app.post("/v1/hotspots/{hotspot_id}/authority", dependencies=[Depends(require_admin)])
def set_hotspot_authority(hotspot_id: str, body: dict, s: Session = Depends(get_session)):
    """Engineer sets the right authority; area memory reuses it for later potholes on this road
    (or this whole area with whole_area=true)."""
    try:
        h = wf.set_authority(s, hotspot_id, str(body.get("authority_key", "")), bool(body.get("whole_area")))
    except wf.WorkflowError as e:
        _bad(e)
    s.commit()
    return {"hotspot_id": h.id, "authority_key": h.authority_key}


@app.get("/v1/admin/tinyfish-check", dependencies=[Depends(require_admin)])
def tinyfish_check():
    """One cheap Search call: proves the API key and network work before the live demo."""
    from .tinyfish.client import TinyFishError, from_settings
    try:
        hits = from_settings(settings).search("pothole complaint municipal corporation Delhi", location="IN")
    except TinyFishError as e:
        raise HTTPException(502, f"TinyFish error: {e}")
    return {"mode": settings.tinyfish_mode, "ok": True, "results": len(hits),
            "first": (hits[0].get("url") if hits else None), "portal_base": settings.portal_base,
            "webhooks": settings.webhooks_enabled}


# ------------------------------------------------- demo portal pass-through
PORTAL_PUBLIC = {"new", "escalate", "status"}


@app.api_route("/portal/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def portal_proxy(path: str, request: Request):
    """The fictional portal lives in the mock container. TinyFish's cloud browser can only reach what the
    tunnel exposes, so the public pages are passed through here. /portal/admin is NOT exposed."""
    import requests
    from starlette.concurrency import run_in_threadpool
    if path.split("/", 1)[0] not in PORTAL_PUBLIC:
        raise HTTPException(404, "not found")
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() in ("content-type", "accept", "user-agent")}
    url = f"{settings.mock_base}/portal/{path}"

    def call():
        return requests.request(request.method, url, params=dict(request.query_params), data=body,
                                headers=headers, allow_redirects=False, timeout=20)
    try:
        r = await run_in_threadpool(call)
    except requests.RequestException as e:
        raise HTTPException(502, f"demo portal unreachable: {e}")
    out = Response(content=r.content, status_code=r.status_code,
                   media_type=r.headers.get("content-type", "text/html"))
    if "location" in r.headers:
        out.headers["location"] = r.headers["location"]
    return out
