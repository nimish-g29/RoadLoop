from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RiderIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    phone: str | None = Field(default=None, pattern=r"^\+?\d{10,13}$")
    consent: bool = Field(description="Rider accepted: on-device processing, hazard-frame upload, "
                                      "and complaints filed only after their approval")


class RiderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    display_name: str


class DetectionMeta(BaseModel):
    rider_id: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    area_frac: float = Field(ge=0, le=1)
    model_conf: float = Field(ge=0, le=1)
    jolt_g: float | None = Field(default=None, ge=0, le=8)
    speed_kmh: float | None = Field(default=None, ge=0, le=200)
    gps_accuracy_m: float | None = Field(default=None, ge=0)
    road_name: str | None = None
    road_ref: str | None = None
    address: str | None = None
    city_key: str | None = None
    state_key: str | None = None
    captured_at: datetime | None = None
    client_id: str | None = Field(default=None, max_length=64)
    source: Literal["auto", "manual", "jolt"] = "auto"


class DetectionOut(BaseModel):
    detection_id: str
    hotspot_id: str
    hotspot_state: str
    severity: str


class PassIn(BaseModel):
    rider_id: str
    lat: float
    lon: float


class ApproveIn(BaseModel):
    rider_id: str


class ComplaintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    portal_key: str
    kind: str
    status: str
    portal_complaint_id: str | None
    portal_status: str | None
    portal_status_label: str | None
    photo_attached: bool
    needs_human_reason: str | None
    reason: str | None
    filed_at: datetime | None
    last_checked_at: datetime | None


class HotspotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    lat: float
    lon: float
    state: str
    severity: str
    score: float
    confidence: float
    distinct_riders: int
    detection_count: int
    priority: float
    road_name: str | None
    address: str | None
    authority_key: str | None
    escalation_level: int
    best_photo_url: str | None = None
    recurrence_of_id: str | None
    first_seen: datetime
    last_seen: datetime
    confirmed_at: datetime | None
    fixed_at: datetime | None
    complaint: ComplaintOut | None = None


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    actor: str
    action: str
    detail: dict | None
    created_at: datetime


class HotspotDetail(HotspotOut):
    complaints: list[ComplaintOut]
    timeline: list[AuditOut]
    photos: list[str]
