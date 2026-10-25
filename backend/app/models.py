from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uid() -> str:
    return uuid.uuid4().hex


class Rider(Base):
    __tablename__ = "riders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    display_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(20))           # used only to file complaints they approve
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Hotspot(Base):
    __tablename__ = "hotspots"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    geohash7: Mapped[str] = mapped_column(String(7), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True, default="candidate")
    severity: Mapped[str] = mapped_column(String(10), default="low")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    distinct_riders: Mapped[int] = mapped_column(Integer, default=0)
    detection_count: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    road_name: Mapped[str | None] = mapped_column(String(200))
    road_ref: Mapped[str | None] = mapped_column(String(40))
    road_class: Mapped[str] = mapped_column(String(16), default="local")
    address: Mapped[str | None] = mapped_column(String(300))
    city_key: Mapped[str | None] = mapped_column(String(40))
    state_key: Mapped[str | None] = mapped_column(String(8))
    authority_key: Mapped[str | None] = mapped_column(String(40))
    authority_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    best_photo: Mapped[str | None] = mapped_column(String(300))
    escalation_level: Mapped[int] = mapped_column(Integer, default=0)
    recurrence_of_id: Mapped[str | None] = mapped_column(ForeignKey("hotspots.id"))

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fixed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    awaiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    detections: Mapped[list[Detection]] = relationship(back_populates="hotspot", order_by="Detection.captured_at")
    complaints: Mapped[list[Complaint]] = relationship(back_populates="hotspot", order_by="Complaint.created_at")


class Detection(Base):
    __tablename__ = "detections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    client_id: Mapped[str | None] = mapped_column(String(64), unique=True)   # idempotency key from the phone
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"), index=True)
    hotspot_id: Mapped[str | None] = mapped_column(ForeignKey("hotspots.id"), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    gps_accuracy_m: Mapped[float | None] = mapped_column(Float)
    speed_kmh: Mapped[float | None] = mapped_column(Float)
    area_frac: Mapped[float] = mapped_column(Float)
    model_conf: Mapped[float] = mapped_column(Float)
    jolt_g: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(10))
    photo: Mapped[str | None] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(10), default="auto")       # auto | manual | jolt
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    hotspot: Mapped[Hotspot | None] = relationship(back_populates="detections")


class PassObservation(Base):
    """A rider passing a hotspot that awaits repair verification."""
    __tablename__ = "pass_observations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    hotspot_id: Mapped[str] = mapped_column(ForeignKey("hotspots.id"), index=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"))
    saw_pothole: Mapped[bool] = mapped_column(Boolean)
    model_conf: Mapped[float] = mapped_column(Float, default=0.0)
    jolt_g: Mapped[float | None] = mapped_column(Float)
    round: Mapped[int] = mapped_column(Integer, default=0)      # escalation round it belongs to
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Complaint(Base):
    __tablename__ = "complaints"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    hotspot_id: Mapped[str] = mapped_column(ForeignKey("hotspots.id"), index=True)
    portal_key: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(16), default="original")         # original | escalation
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("complaints.id"))
    approved_by: Mapped[str] = mapped_column(ForeignKey("riders.id"))
    portal_complaint_id: Mapped[str | None] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(20), default="filing")  # filing|filed|needs_human|failed|closed|superseded
    portal_status: Mapped[str | None] = mapped_column(String(20))
    portal_status_label: Mapped[str | None] = mapped_column(String(120))
    photo_attached: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_human_reason: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(Text)                          # why an escalation was raised
    monitor_id: Mapped[str | None] = mapped_column(String(64), index=True)
    filed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    hotspot: Mapped[Hotspot] = relationship(back_populates="complaints")


class AgentRun(Base):
    """Every TinyFish call that does work on a portal: the audit trail judges can inspect."""
    __tablename__ = "agent_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(20))           # file | escalate | status | authority
    hotspot_id: Mapped[str | None] = mapped_column(ForeignKey("hotspots.id"), index=True)
    complaint_id: Mapped[str | None] = mapped_column(ForeignKey("complaints.id"))
    url: Mapped[str] = mapped_column(String(500))
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[Any] = mapped_column(JSON, nullable=True)
    error: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hotspot_id: Mapped[str | None] = mapped_column(ForeignKey("hotspots.id"), index=True)
    actor: Mapped[str] = mapped_column(String(60))           # system | rider:<id> | tinyfish:<run> | monitor:<id>
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[Any] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AreaAuthority(Base):
    """Area memory: which authority maintains a road around here. Learned once (rules, TinyFish or an
    engineer's correction) and reused for every later pothole nearby, so TinyFish only researches
    roads RoadLoop has never seen and the agent just files."""
    __tablename__ = "area_authorities"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    cell: Mapped[str] = mapped_column(String(8), index=True)                 # geohash, AREA_PRECISION
    road_key: Mapped[str | None] = mapped_column(String(120), index=True)    # "nh44" / "aurobindo_marg"; None = any road
    road_name: Mapped[str | None] = mapped_column(String(200))
    city_key: Mapped[str | None] = mapped_column(String(40))
    state_key: Mapped[str | None] = mapped_column(String(8))
    authority_key: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(16))                          # rules | tinyfish | engineer
    evidence_url: Mapped[str | None] = mapped_column(String(500))
    hits: Mapped[int] = mapped_column(Integer, default=0)                    # reuses = TinyFish runs saved
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AreaPlace(Base):
    """Reverse-geocoding cache per ~150 m cell, so a busy road is geocoded once, not per pothole."""
    __tablename__ = "area_places"
    cell: Mapped[str] = mapped_column(String(8), primary_key=True)          # geohash precision 7
    place: Mapped[Any] = mapped_column(JSON)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
