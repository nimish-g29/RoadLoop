"""Celery worker + beat. Each task: open a session, call workflow, commit, then enqueue follow-ups.

Run:  celery -A app.worker.celery_app worker -l info
      celery -A app.worker.celery_app beat -l info
"""
from __future__ import annotations

import logging

from celery import Celery

from .config import settings
from .db import SessionLocal
from .models import Complaint, Hotspot
from .core.lifecycle import Event, State
from .services import workflow as wf
from .tinyfish import goals
from .tinyfish.client import TinyFishError, from_settings

log = logging.getLogger("roadloop.worker")

celery_app = Celery("roadloop", broker=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    timezone="Asia/Kolkata",
    beat_schedule={
        "status-checks": {"task": "roadloop.check_statuses", "schedule": 30 * 60},
        "sla": {"task": "roadloop.check_sla", "schedule": 60 * 60},
        "priorities": {"task": "roadloop.refresh_priorities", "schedule": 15 * 60},
    },
)


def tf():
    return from_settings(settings)


def enqueue(jobs: list[tuple[str, dict]]) -> None:
    for name, kwargs in jobs:
        kwargs = dict(kwargs)
        countdown = kwargs.pop("_countdown", None)
        celery_app.tasks[name].apply_async(kwargs=kwargs, countdown=countdown)


def _run(fn, *args, **kwargs):
    with SessionLocal() as s:
        jobs = fn(s, *args, **kwargs) or []
        s.commit()
    enqueue(jobs)


@celery_app.task(name="roadloop.file_complaint", bind=True, max_retries=3)
def file_complaint(self, complaint_id: str):
    try:
        _run(wf.start_filing, complaint_id, tf())
    except TinyFishError as e:
        if e.retryable and self.request.retries < self.max_retries:
            raise self.retry(countdown=e.retry_after or 30 * (self.request.retries + 1), exc=e)
        with SessionLocal() as s:
            c = s.get(Complaint, complaint_id)
            c.status, c.needs_human_reason = "failed", f"TinyFish error: {e}"
            if c.kind == "original" and c.hotspot.state == State.FILING.value:
                wf.transition(s, c.hotspot, Event.FILED_FAIL, "system", reason=str(e))
            s.commit()


@celery_app.task(name="roadloop.poll_run", bind=True, max_retries=60)
def poll_run(self, run_id: str):
    try:
        run = tf().get_run(run_id)
    except TinyFishError as e:
        raise self.retry(countdown=30, exc=e)
    _run(wf.handle_run, run)


@celery_app.task(name="roadloop.start_tracking")
def start_tracking(complaint_id: str):
    _run(wf.start_tracking, complaint_id, tf())


@celery_app.task(name="roadloop.stop_monitor")
def stop_monitor(monitor_id: str):
    try:
        tf().delete_monitor(monitor_id)  # frees a slot under the per-account monitor cap
    except TinyFishError as e:
        log.warning("could not delete monitor %s: %s", monitor_id, e)


@celery_app.task(name="roadloop.resolve_authority")
def resolve_authority(hotspot_id: str):
    client = tf()
    with SessionLocal() as s:
        h = s.get(Hotspot, hotspot_id)
        if not h:
            return
        road = h.road_name or h.road_ref or f"the road at {h.lat:.5f},{h.lon:.5f}"
        city = h.city_key or "India"
        # Search first (cheap); an agent run reads the best official page and returns a typed answer.
        hits = client.search(f"{road} {city} road maintenance authority complaint", location="IN")
        official = next((r["url"] for r in hits if ".gov.in" in r.get("url", "") or ".nic.in" in r.get("url", "")),
                        hits[0]["url"] if hits else None)
        if not official:
            wf.audit(s, h.id, "tinyfish:search", "authority_unresolved", query=road)
            s.commit()
            return
        goal = goals.authority_goal(road, city)
        run_id = client.run_async(official, goal, goals.AUTHORITY_SCHEMA,
                                  webhook_url=settings.webhook_url("agent") if settings.webhooks_enabled else None)
        from .models import AgentRun
        s.add(AgentRun(run_id=run_id, purpose="authority", hotspot_id=h.id, url=official, goal=goal))
        s.commit()
    enqueue([("roadloop.poll_run", {"run_id": run_id, "_countdown": 30})])


@celery_app.task(name="roadloop.geocode_hotspot", bind=True, max_retries=3, rate_limit="1/s")
def geocode_hotspot(self, hotspot_id: str):
    from .services import geocode
    with SessionLocal() as s:
        h = s.get(Hotspot, hotspot_id)
        if not h:
            return
        lat, lon = h.lat, h.lon
    try:
        place = geocode.reverse(lat, lon, settings.geocoder_user_agent)
    except Exception as e:  # network / rate limit: retry later, never block the pipeline
        raise self.retry(countdown=10 * (self.request.retries + 1), exc=e)
    _run(wf.apply_geocode, hotspot_id, place)


@celery_app.task(name="roadloop.check_statuses")
def check_statuses():
    client = tf()
    with SessionLocal() as s:
        to_fetch, to_lookup = wf.due_status_checks(s)
        jobs = wf.check_by_fetch(s, to_fetch, client) if to_fetch else []
        for c in to_lookup:
            jobs += wf.start_status_lookup(s, c, client)
        s.commit()
    enqueue(jobs)


@celery_app.task(name="roadloop.check_sla")
def check_sla():
    _run(wf.check_sla)


@celery_app.task(name="roadloop.refresh_priorities")
def refresh_priorities():
    _run(wf.refresh_priorities)
