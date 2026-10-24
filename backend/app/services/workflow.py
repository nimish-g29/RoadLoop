"""RoadLoop's business logic: ingest -> merge -> confirm -> file -> track -> verify -> escalate.

Every function takes a Session and returns a list of follow-up jobs
(task name, kwargs). Callers commit FIRST and enqueue after, so a worker never
picks up a job for a row that isn't committed yet. No function here talks to
Celery directly, which keeps this module testable with a plain database.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..core import geo, scoring
from ..core.lifecycle import (TRACKED, Event, PassObservation as Obs, PortalStatus, State,
                              classify_status_label, decide_verification, next_state,
                              parse_portal_status, sla_breached)
from ..core.routing import resolve_authority, road_class_from_ref
from ..models import AgentRun, AuditEvent, Complaint, Detection, Hotspot, PassObservation, Rider
from ..tinyfish import goals
from ..tinyfish.client import AgentGoalFailed, TinyFish, TinyFishError

Job = tuple[str, dict]
RECURRENCE_WINDOW = timedelta(days=180)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def audit(s: Session, hotspot_id: str | None, actor: str, action: str, **detail) -> None:
    s.add(AuditEvent(hotspot_id=hotspot_id, actor=actor, action=action, detail=detail))


def transition(s: Session, h: Hotspot, event: Event, actor: str, **detail) -> State:
    old = State(h.state)
    new = next_state(old, event)
    h.state = new.value
    audit(s, h.id, actor, f"state:{old.value}->{new.value}", event=event.value, **detail)
    return new


class WorkflowError(ValueError):
    """Bad request from a client (maps to HTTP 4xx)."""


def portals():
    return goals.load_portals(settings.mock_base)


# ================================================================== ingest
@dataclass
class DetectionIn:
    rider_id: str
    lat: float
    lon: float
    area_frac: float
    model_conf: float
    jolt_g: float | None = None
    speed_kmh: float | None = None
    gps_accuracy_m: float | None = None
    road_name: str | None = None
    road_ref: str | None = None
    address: str | None = None
    city_key: str | None = None
    state_key: str | None = None
    captured_at: datetime | None = None
    client_id: str | None = None
    source: str = "auto"          # auto = drive-mode model, manual = photo report, jolt = hands-free bump capture


def ingest_detection(s: Session, d: DetectionIn, photo: str | None) -> tuple[Detection, list[Job]]:
    if d.client_id:
        existing = s.scalar(select(Detection).where(Detection.client_id == d.client_id))
        if existing:
            return existing, []  # phone retried an upload; idempotent
    if not s.get(Rider, d.rider_id):
        raise WorkflowError("unknown rider")
    if not (0 <= d.area_frac <= 1 and 0 <= d.model_conf <= 1):
        raise WorkflowError("area_frac and model_conf must be in [0, 1]")

    h = _match_hotspot(s, d)
    jobs: list[Job] = []
    created = False
    if h is None or h.state == State.VERIFIED_FIXED.value:
        recurrence = h if h is not None and aware(h.fixed_at) and now_utc() - aware(h.fixed_at) < RECURRENCE_WINDOW else None
        h = Hotspot(lat=d.lat, lon=d.lon, geohash7=geo.encode(d.lat, d.lon, 7), state=State.CANDIDATE.value,
                    recurrence_of_id=recurrence.id if recurrence else None)
        s.add(h)
        s.flush()
        created = True
        audit(s, h.id, f"rider:{d.rider_id}", "hotspot_created", source=d.source,
              recurrence_of=recurrence.id if recurrence else None)
    for attr in ("road_name", "road_ref", "address", "city_key", "state_key"):
        if getattr(d, attr):
            setattr(h, attr, getattr(d, attr))
    # Road class comes from the hotspot (which may have been geocoded), not just this upload.
    h.road_class = road_class_from_ref(h.road_ref, h.road_name)
    score = scoring.detection_score(scoring.DetectionSignal(d.area_frac, d.model_conf, d.jolt_g,
                                                            d.speed_kmh, h.road_class))
    det = Detection(rider_id=d.rider_id, hotspot_id=h.id, lat=d.lat, lon=d.lon, gps_accuracy_m=d.gps_accuracy_m,
                    speed_kmh=d.speed_kmh, area_frac=d.area_frac, model_conf=d.model_conf, jolt_g=d.jolt_g,
                    score=score, severity=scoring.classify(score).value, photo=photo, source=d.source,
                    captured_at=d.captured_at or now_utc(), client_id=d.client_id)
    s.add(det)
    s.flush()
    recompute_hotspot(s, h)
    if created and not h.address and settings.geocoder != "off":
        jobs.append(("roadloop.geocode_hotspot", {"hotspot_id": h.id}))

    if h.state == State.AWAITING_VERIFICATION.value:
        _record_obs(s, h, d.rider_id, saw=True, conf=d.model_conf, jolt=d.jolt_g)
        jobs += evaluate_verification(s, h)
    elif h.state == State.CANDIDATE.value and scoring.eligible_to_file(
            h.distinct_riders, h.confidence, scoring.FilingPolicy(settings.min_riders, settings.min_confidence)):
        transition(s, h, Event.CONFIRM, "system", riders=h.distinct_riders, confidence=h.confidence)
        h.confirmed_at = now_utc()
        jobs += _resolve_authority(s, h)
    h.priority = compute_priority(s, h)
    return det, jobs


def _match_hotspot(s: Session, d: DetectionIn) -> Hotspot | None:
    """Nearest hotspot within the merge radius; active hotspots win over already-fixed ones."""
    radius = max(settings.merge_radius_m, min(d.gps_accuracy_m or 0, 20))
    cells = geo.neighbours(geo.encode(d.lat, d.lon, 7))
    near = []
    for h in s.scalars(select(Hotspot).where(Hotspot.geohash7.in_(cells))):
        dist = geo.haversine_m(d.lat, d.lon, h.lat, h.lon)
        if dist <= radius:
            near.append((h.state == State.VERIFIED_FIXED.value, dist, h))
    return min(near, key=lambda t: (t[0], t[1]))[2] if near else None


def recompute_hotspot(s: Session, h: Hotspot) -> None:
    dets = list(s.scalars(select(Detection).where(Detection.hotspot_id == h.id)))
    if not dets:
        return
    best_by_rider: dict[str, float] = {}
    for x in dets:
        best_by_rider[x.rider_id] = max(best_by_rider.get(x.rider_id, 0.0), x.model_conf)
    h.score = scoring.hotspot_score([x.score for x in dets])
    h.severity = scoring.classify(h.score).value
    h.confidence = scoring.crowd_confidence(best_by_rider)
    h.distinct_riders = len(best_by_rider)
    h.detection_count = len(dets)
    if h.state in (State.CANDIDATE.value, State.CONFIRMED.value):
        # Location settles while the crowd is still confirming; frozen once filed.
        w = sum(x.model_conf for x in dets) or 1.0
        h.lat = sum(x.lat * x.model_conf for x in dets) / w
        h.lon = sum(x.lon * x.model_conf for x in dets) / w
        h.geohash7 = geo.encode(h.lat, h.lon, 7)
    with_photo = [x for x in dets if x.photo]
    if with_photo:
        h.best_photo = max(with_photo, key=lambda x: x.score).photo
    h.first_seen = min(aware(x.captured_at) for x in dets)
    h.last_seen = max(aware(x.captured_at) for x in dets)


def compute_priority(s: Session, h: Hotspot, now: datetime | None = None) -> float:
    if h.state == State.VERIFIED_FIXED.value:
        return 0.0
    now = now or now_utc()
    since = now - timedelta(days=7)
    passes = (s.scalar(select(func.count()).select_from(Detection)
                       .where(Detection.hotspot_id == h.id, Detection.captured_at >= since)) or 0)
    passes += (s.scalar(select(func.count()).select_from(PassObservation)
                        .where(PassObservation.hotspot_id == h.id, PassObservation.created_at >= since)) or 0)
    opened = aware(h.confirmed_at) or aware(h.first_seen) or now
    days_open = (now - opened).total_seconds() / 86400
    return scoring.priority(scoring.Severity(h.severity), h.road_class, passes, days_open)


def _resolve_authority(s: Session, h: Hotspot) -> list[Job]:
    r = resolve_authority(h.road_ref, h.road_name, h.city_key, h.state_key)
    h.authority_key, h.authority_confidence = r.authority_key, r.confidence
    audit(s, h.id, "system", "authority_rules", authority=r.authority_key, confidence=r.confidence, reason=r.reason)
    return [("roadloop.resolve_authority", {"hotspot_id": h.id})] if r.search_query else []


# ============================================================ approve + file
def pick_portal(h: Hotspot) -> str:
    p = portals()
    if h.authority_key and h.authority_key in p and p[h.authority_key].usable:
        return h.authority_key
    return settings.default_portal


def approve_filing(s: Session, hotspot_id: str, rider_id: str) -> tuple[Complaint, list[Job]]:
    h = s.get(Hotspot, hotspot_id)
    rider = s.get(Rider, rider_id)
    if not h or not rider:
        raise WorkflowError("unknown hotspot or rider")
    if not rider.phone:
        raise WorkflowError("rider has no phone number on file; portals need one")
    witnessed = s.scalar(select(func.count()).select_from(Detection)
                         .where(Detection.hotspot_id == h.id, Detection.rider_id == rider_id))
    if not witnessed:
        raise WorkflowError("only riders who reported this pothole can file it under their name")
    if h.state == State.CONFIRMED.value:
        transition(s, h, Event.APPROVE, f"rider:{rider_id}")
    elif h.state == State.FILING_FAILED.value:
        transition(s, h, Event.RETRY, f"rider:{rider_id}")
    else:
        raise WorkflowError(f"hotspot is '{h.state}', nothing to approve")
    c = Complaint(hotspot_id=h.id, portal_key=pick_portal(h), approved_by=rider_id, kind="original")
    s.add(c)
    s.flush()
    audit(s, h.id, f"rider:{rider_id}", "approved_filing", portal=c.portal_key, complaint=c.id)
    return c, [("roadloop.file_complaint", {"complaint_id": c.id})]


def _complaint_payload(s: Session, c: Complaint) -> goals.Complaint:
    h, rider = c.hotspot, s.get(Rider, c.approved_by)
    photo = settings.media_url(h.best_photo) if h.best_photo else "(photo unavailable)"
    return goals.Complaint(
        citizen_name=rider.display_name, citizen_phone=rider.phone or "",
        lat=h.lat, lon=h.lon, address=h.address or h.road_name or f"{h.lat:.5f},{h.lon:.5f}",
        severity=h.severity, first_seen=aware(h.first_seen).strftime("%d %b %Y"),
        reports=h.distinct_riders, photo_url=photo)


def start_filing(s: Session, complaint_id: str, tf: TinyFish) -> list[Job]:
    """Kick off the agent run that files (or escalates) a complaint."""
    c = s.get(Complaint, complaint_id)
    if not c or c.status != "filing":
        return []
    p = portals()[c.portal_key]
    payload = _complaint_payload(s, c)
    if c.kind == "escalation":
        parent = s.get(Complaint, c.parent_id)
        days = (now_utc() - (aware(parent.filed_at) or now_utc())).days
        goal = goals.escalate_goal(p, portals()[parent.portal_key].name, parent.portal_complaint_id or "?",
                                   payload, days, c.reason or "The pothole is still present.")
        purpose = "escalate"
    else:
        goal, purpose = goals.file_goal(p, payload), "file"
    hook = settings.webhook_url("agent") if settings.webhooks_enabled else None
    run_id = tf.run_async(p.filing_url, goal, goals.FILE_SCHEMA, webhook_url=hook, browser_profile="stealth")
    s.add(AgentRun(run_id=run_id, purpose=purpose, hotspot_id=c.hotspot_id, complaint_id=c.id,
                   url=p.filing_url, goal=goal))
    audit(s, c.hotspot_id, f"tinyfish:{run_id}", f"agent_{purpose}_started", portal=p.key)
    s.flush()
    # Always schedule a poll as a safety net: webhooks are best effort.
    return [("roadloop.poll_run", {"run_id": run_id, "_countdown": 30 if hook else 10})]


# ============================================================== run results
def handle_run(s: Session, run: dict) -> list[Job]:
    """Process a finished TinyFish run (from webhook or poll). Idempotent."""
    ar = s.get(AgentRun, run.get("run_id"))
    if not ar or ar.handled:
        return []
    ar.status = run.get("status", ar.status)
    if ar.status not in ("COMPLETED", "FAILED", "CANCELLED"):
        return [("roadloop.poll_run", {"run_id": ar.run_id, "_countdown": 15})]
    ar.handled, ar.finished_at = True, now_utc()
    ar.result, ar.error = run.get("result"), run.get("error")
    try:
        res, err = TinyFish.agent_result(run), None
    except (AgentGoalFailed, TinyFishError) as e:
        res, err = None, str(e)
    if ar.purpose in ("file", "escalate"):
        return _after_filing(s, ar, res, err)
    if ar.purpose == "status":
        c = s.get(Complaint, ar.complaint_id)
        if c and res and res.get("found") and res.get("status_label"):
            return apply_portal_status(s, c, classify_status_label(res["status_label"]),
                                       res["status_label"], f"tinyfish:{ar.run_id}")
        if c:
            c.last_checked_at = now_utc()
        return []
    if ar.purpose == "authority":
        h = s.get(Hotspot, ar.hotspot_id)
        if h and res:
            mapping = {"nhai": "nhai", "state_pwd": f"pwd_{h.state_key}" if h.state_key else None,
                       "municipal_corporation": f"city_{h.city_key}" if h.city_key else None}
            key = mapping.get(res.get("authority"))
            if key:
                h.authority_key, h.authority_confidence = key, 0.9
            audit(s, h.id, f"tinyfish:{ar.run_id}", "authority_resolved", **res)
    return []


def _after_filing(s: Session, ar: AgentRun, res: dict | None, err: str | None) -> list[Job]:
    c = s.get(Complaint, ar.complaint_id)
    h = c.hotspot
    actor = f"tinyfish:{ar.run_id}"
    outcome = (res or {}).get("status")
    if outcome == "filed" and res.get("complaint_id"):
        c.status, c.portal_complaint_id = "filed", res["complaint_id"]
        c.photo_attached, c.filed_at = bool(res.get("photo_attached")), now_utc()
        if c.kind == "original":
            transition(s, h, Event.FILED_OK, actor, complaint_id=c.portal_complaint_id)
        else:
            audit(s, h.id, actor, "escalation_filed", complaint_id=c.portal_complaint_id, level=h.escalation_level)
        return [("roadloop.start_tracking", {"complaint_id": c.id})]
    reason = (res or {}).get("blocked_on") or (res or {}).get("portal_message") or err or "unknown"
    c.status = "needs_human" if outcome == "needs_human" else "failed"
    c.needs_human_reason = reason
    if c.kind == "original":
        transition(s, h, Event.FILED_FAIL, actor, reason=reason, outcome=outcome)
    else:
        audit(s, h.id, actor, "escalation_failed", reason=reason)
    return []


# ================================================================== tracking
def start_tracking(s: Session, complaint_id: str, tf: TinyFish) -> list[Job]:
    c = s.get(Complaint, complaint_id)
    if not c or c.status != "filed" or c.monitor_id:
        return []
    p = portals()[c.portal_key]
    if p.status_mode == "public_page" and settings.webhooks_enabled:
        try:
            m = tf.create_page_monitor(
                f"RoadLoop {c.portal_complaint_id}", p.status_url_for(c.portal_complaint_id), settings.monitor_cron,
                purpose="The complaint status changes, especially to Closed, Resolved, Disposed or Rejected",
                webhook_url=settings.webhook_url("monitor"))
            c.monitor_id = m["monitor"]["id"]
            audit(s, c.hotspot_id, f"monitor:{c.monitor_id}", "monitor_created", url=p.status_url_for(c.portal_complaint_id))
        except TinyFishError as e:
            # e.g. the 50-monitor cap: fall back to batched Fetch checks from Celery beat.
            audit(s, c.hotspot_id, "system", "monitor_unavailable", error=str(e))
    return []


def apply_portal_status(s: Session, c: Complaint, ps: PortalStatus | None, label: str | None,
                        actor: str) -> list[Job]:
    c.last_checked_at = now_utc()
    if ps is None:
        return []
    changed = c.portal_status != ps.value
    c.portal_status, c.portal_status_label = ps.value, (label or ps.value)[:120]
    if c.status != "filed":
        return []
    h = c.hotspot
    jobs: list[Job] = []
    if changed:
        audit(s, h.id, actor, "portal_status", status=ps.value, label=label, complaint_id=c.portal_complaint_id)
    if ps == PortalStatus.IN_PROGRESS and h.state == State.FILED.value:
        transition(s, h, Event.PORTAL_IN_PROGRESS, actor)
    elif ps == PortalStatus.CLOSED and State(h.state) in TRACKED:
        transition(s, h, Event.PORTAL_CLOSED, actor, complaint_id=c.portal_complaint_id)
        h.awaiting_since = now_utc()
        c.status = "closed"
        if c.monitor_id:
            jobs.append(("roadloop.stop_monitor", {"monitor_id": c.monitor_id}))
    elif ps == PortalStatus.REJECTED and State(h.state) in TRACKED:
        jobs += escalate(s, h.id, f"The portal rejected complaint {c.portal_complaint_id} "
                                  f"('{label}') although road users still report the pothole.")
    h.priority = compute_priority(s, h)
    return jobs


def on_monitor_webhook(s: Session, payload: dict) -> list[Job]:
    mid = payload.get("fetch_monitor_id")
    if not mid or payload.get("is_baseline"):
        return []
    c = s.scalar(select(Complaint).where(Complaint.monitor_id == mid))
    if not c:
        return []
    results = payload.get("results") or []
    if not results:
        audit(s, c.hotspot_id, f"monitor:{mid}", "monitor_error", errors=payload.get("errors"))
        return []
    text = results[0].get("text") or ""
    ps = parse_portal_status(text, c.portal_complaint_id)
    return apply_portal_status(s, c, ps, ps.value if ps else None, f"monitor:{mid}")


def due_status_checks(s: Session, now: datetime | None = None,
                      force: bool = False) -> tuple[list[Complaint], list[Complaint]]:
    """(complaints to Fetch in batches, complaints needing an Agent lookup).
    force=True (dashboard button) re-checks every open complaint, even monitored or recently checked ones."""
    now = now or now_utc()
    stale = now - timedelta(hours=settings.status_check_hours)
    p = portals()
    fetch, agent = [], []
    for c in s.scalars(select(Complaint).where(Complaint.status == "filed")):
        if not force and (c.monitor_id or (c.last_checked_at and aware(c.last_checked_at) > stale)):
            continue
        portal = p.get(c.portal_key)
        if not portal:
            continue
        (fetch if portal.status_mode == "public_page" else agent).append(c)
    return fetch, agent


def check_by_fetch(s: Session, complaints: list[Complaint], tf: TinyFish) -> list[Job]:
    p = portals()
    jobs: list[Job] = []
    for i in range(0, len(complaints), 10):  # Fetch takes up to 10 URLs per call
        batch = complaints[i:i + 10]
        urls = {p[c.portal_key].status_url_for(c.portal_complaint_id): c for c in batch}
        data = tf.fetch(list(urls), ttl=0, purpose="Read grievance status")
        for r in data.get("results", []):
            c = urls.get(r.get("url"))
            if c:
                ps = parse_portal_status(r.get("text") or "", c.portal_complaint_id)
                jobs += apply_portal_status(s, c, ps, ps.value if ps else None, "tinyfish:fetch")
    return jobs


def start_status_lookup(s: Session, c: Complaint, tf: TinyFish) -> list[Job]:
    p = portals()[c.portal_key]
    goal = goals.status_goal(c.portal_complaint_id)
    hook = settings.webhook_url("agent") if settings.webhooks_enabled else None
    run_id = tf.run_async(p.status_url or p.filing_url, goal, goals.STATUS_SCHEMA, webhook_url=hook)
    s.add(AgentRun(run_id=run_id, purpose="status", hotspot_id=c.hotspot_id, complaint_id=c.id,
                   url=p.status_url or p.filing_url, goal=goal))
    c.last_checked_at = now_utc()
    s.flush()
    return [("roadloop.poll_run", {"run_id": run_id, "_countdown": 30})]


# ============================================================ verification
def _record_obs(s: Session, h: Hotspot, rider_id: str, saw: bool, conf: float = 0.0, jolt: float | None = None):
    s.add(PassObservation(hotspot_id=h.id, rider_id=rider_id, saw_pothole=saw, model_conf=conf, jolt_g=jolt,
                          round=h.escalation_level))
    s.flush()


def record_pass(s: Session, hotspot_id: str, rider_id: str, lat: float, lon: float) -> list[Job]:
    """Rider drove through a hotspot awaiting verification and the detector saw nothing."""
    h = s.get(Hotspot, hotspot_id)
    if not h or not s.get(Rider, rider_id):
        raise WorkflowError("unknown hotspot or rider")
    if h.state != State.AWAITING_VERIFICATION.value:
        return []
    if geo.haversine_m(lat, lon, h.lat, h.lon) > settings.verify_radius_m:
        raise WorkflowError("pass is too far from the hotspot to count")
    _record_obs(s, h, rider_id, saw=False)
    return evaluate_verification(s, h)


def evaluate_verification(s: Session, h: Hotspot) -> list[Job]:
    since = aware(h.awaiting_since) or datetime.min.replace(tzinfo=timezone.utc)
    rows = [o for o in s.scalars(select(PassObservation).where(
        PassObservation.hotspot_id == h.id, PassObservation.round == h.escalation_level))
        if aware(o.created_at) >= since]
    # Latest observation per rider wins.
    latest: dict[str, PassObservation] = {}
    for o in sorted(rows, key=lambda o: aware(o.created_at)):
        latest[o.rider_id] = o
    ev = decide_verification([Obs(o.rider_id, o.saw_pothole, o.model_conf, o.jolt_g) for o in latest.values()])
    if ev == Event.ROAD_CLEAR:
        transition(s, h, ev, "crowd", riders=len(latest))
        h.fixed_at, h.priority = now_utc(), 0.0
        return []
    if ev == Event.ROAD_STILL_BAD:
        transition(s, h, ev, "crowd", riders=sum(o.saw_pothole for o in latest.values()))
        closed = s.scalar(select(Complaint).where(Complaint.hotspot_id == h.id, Complaint.status == "closed")
                          .order_by(Complaint.created_at.desc()))
        cid = closed.portal_complaint_id if closed else "?"
        return escalate(s, h.id, f"The portal marked complaint {cid} as closed, but "
                                 f"{sum(o.saw_pothole for o in latest.values())} road users still detected "
                                 f"the pothole on {now_utc():%d %b %Y}.")
    return []


# ================================================================ escalation
def escalate(s: Session, hotspot_id: str, reason: str) -> list[Job]:
    h = s.get(Hotspot, hotspot_id)
    current = s.scalar(select(Complaint).where(Complaint.hotspot_id == h.id,
                                               Complaint.status.in_(("filed", "closed")))
                       .order_by(Complaint.created_at.desc()))
    if not current:
        return []
    if h.escalation_level >= settings.max_escalations:
        audit(s, h.id, "system", "escalation_cap_reached", reason=reason)
        return []
    transition(s, h, Event.ESCALATE, "system", reason=reason)
    h.escalation_level += 1
    h.awaiting_since = None
    jobs: list[Job] = []
    if current.monitor_id and current.status == "filed":
        jobs.append(("roadloop.stop_monitor", {"monitor_id": current.monitor_id}))
    current.status = "superseded"
    target = portals()[current.portal_key].escalate_to or current.portal_key
    child = Complaint(hotspot_id=h.id, portal_key=target, kind="escalation", parent_id=current.id,
                      approved_by=current.approved_by, reason=reason)
    s.add(child)
    s.flush()
    audit(s, h.id, "system", "escalation_queued", target=target, level=h.escalation_level, reason=reason)
    jobs.append(("roadloop.file_complaint", {"complaint_id": child.id}))
    return jobs


def check_sla(s: Session, now: datetime | None = None) -> list[Job]:
    now = now or now_utc()
    jobs: list[Job] = []
    for c in list(s.scalars(select(Complaint).where(Complaint.status == "filed"))):
        h = c.hotspot
        if State(h.state) in TRACKED and c.filed_at and sla_breached(aware(c.filed_at), now, settings.sla_days):
            jobs += escalate(s, h.id, f"No repair within {settings.sla_days} days of complaint "
                                      f"{c.portal_complaint_id}.")
    return jobs


def apply_geocode(s: Session, hotspot_id: str, place: dict) -> list[Job]:
    """Fill road/address fields from reverse geocoding without overwriting what riders sent."""
    h = s.get(Hotspot, hotspot_id)
    if not h or not place:
        return []
    for attr in ("road_name", "road_ref", "address", "city_key", "state_key"):
        if place.get(attr) and not getattr(h, attr):
            setattr(h, attr, place[attr])
    h.road_class = road_class_from_ref(h.road_ref, h.road_name)
    audit(s, h.id, "geocoder", "geocoded", **{k: v for k, v in place.items() if v})
    jobs: list[Job] = []
    if h.state == State.CONFIRMED.value and not h.authority_key:
        jobs += _resolve_authority(s, h)
    h.priority = compute_priority(s, h)
    return jobs


def refresh_priorities(s: Session) -> None:
    for h in s.scalars(select(Hotspot).where(Hotspot.state != State.VERIFIED_FIXED.value)):
        h.priority = compute_priority(s, h)
