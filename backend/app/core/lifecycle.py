"""Hotspot lifecycle: state machine, portal-status parsing, repair verification.

    candidate --(crowd confirms)--> confirmed --(rider approves)--> filing
    filing --(agent returns complaint ID)--> filed      | --(agent fails)--> filing_failed
    filed / in_progress --(portal says closed)--> awaiting_verification
    awaiting_verification --(riders see road clear)--> verified_fixed
    awaiting_verification --(riders still see pothole)--> reopened --> escalated
    filed / in_progress --(SLA missed)--> escalated
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class State(str, Enum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    FILING = "filing"
    FILING_FAILED = "filing_failed"
    FILED = "filed"
    IN_PROGRESS = "in_progress"
    AWAITING_VERIFICATION = "awaiting_verification"
    VERIFIED_FIXED = "verified_fixed"
    REOPENED = "reopened"
    ESCALATED = "escalated"


class Event(str, Enum):
    CONFIRM = "confirm"
    APPROVE = "approve"
    FILED_OK = "filed_ok"
    FILED_FAIL = "filed_fail"
    RETRY = "retry"
    PORTAL_IN_PROGRESS = "portal_in_progress"
    PORTAL_CLOSED = "portal_closed"
    ROAD_CLEAR = "road_clear"
    ROAD_STILL_BAD = "road_still_bad"
    ESCALATE = "escalate"


S, E = State, Event
TRANSITIONS: dict[tuple[State, Event], State] = {
    (S.CANDIDATE, E.CONFIRM): S.CONFIRMED,
    (S.CONFIRMED, E.APPROVE): S.FILING,
    (S.FILING, E.FILED_OK): S.FILED,
    (S.FILING, E.FILED_FAIL): S.FILING_FAILED,
    (S.FILING_FAILED, E.RETRY): S.FILING,
    (S.FILED, E.PORTAL_IN_PROGRESS): S.IN_PROGRESS,
    (S.FILED, E.PORTAL_CLOSED): S.AWAITING_VERIFICATION,
    (S.IN_PROGRESS, E.PORTAL_CLOSED): S.AWAITING_VERIFICATION,
    (S.AWAITING_VERIFICATION, E.ROAD_CLEAR): S.VERIFIED_FIXED,
    (S.AWAITING_VERIFICATION, E.ROAD_STILL_BAD): S.REOPENED,
    (S.REOPENED, E.ESCALATE): S.ESCALATED,
    (S.FILED, E.ESCALATE): S.ESCALATED,
    (S.IN_PROGRESS, E.ESCALATE): S.ESCALATED,
    (S.ESCALATED, E.PORTAL_CLOSED): S.AWAITING_VERIFICATION,
    (S.ESCALATED, E.PORTAL_IN_PROGRESS): S.ESCALATED,
    (S.ESCALATED, E.ESCALATE): S.ESCALATED,          # next escalation level
}

# States whose complaint is "live" on a portal and should be watched.
TRACKED = {S.FILED, S.IN_PROGRESS, S.ESCALATED}


class InvalidTransition(ValueError):
    pass


def next_state(current: State, event: Event) -> State:
    try:
        return TRANSITIONS[(State(current), Event(event))]
    except KeyError:
        raise InvalidTransition(f"{current} --{event}--> ?") from None


# ---------------------------------------------------------------- portal text
class PortalStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"
    REJECTED = "rejected"


# Order matters: more specific phrases first. Includes common Hindi labels.
_PATTERNS: list[tuple[PortalStatus, re.Pattern]] = [
    (PortalStatus.REJECTED, re.compile(r"\b(rejected|not\s+feasible|invalid\s+complaint)\b|अस्वीकृत", re.I)),
    (PortalStatus.CLOSED, re.compile(
        r"\b(closed|resolved|disposed(\s+of)?|work\s+completed|completed|attended|redressed)\b|निस्तारित|निराकृत|बंद", re.I)),
    (PortalStatus.IN_PROGRESS, re.compile(
        r"\b(in\s+progress|under\s+(process|progress|review)|work\s+(started|order\s+issued)|forwarded|assigned)\b|प्रक्रियाधीन",
        re.I)),
    (PortalStatus.OPEN, re.compile(r"\b(pending|open|registered|submitted|received)\b|लंबित|दर्ज", re.I)),
]
_LABELLED = re.compile(r"(?:status|स्थिति)\s*[:\-–]\s*([^\n|]{2,60})", re.I)


def classify_status_label(label: str) -> PortalStatus | None:
    for status, pat in _PATTERNS:
        if pat.search(label):
            return status
    return None


def parse_portal_status(text: str, complaint_id: str | None = None) -> PortalStatus | None:
    """Best-effort status from a fetched status page (markdown/text).

    If a complaint ID is given we only look at the text around it, because
    tracker pages often list several complaints."""
    if not text:
        return None
    window = text
    if complaint_id:
        idx = text.find(complaint_id)
        if idx == -1:
            return None
        window = text[max(0, idx - 300): idx + 600]
    m = _LABELLED.search(window)
    if m:
        found = classify_status_label(m.group(1))
        if found:
            return found
    return classify_status_label(window)


# ------------------------------------------------------------- verification
@dataclass(frozen=True)
class PassObservation:
    rider_id: str
    saw_pothole: bool
    model_conf: float = 0.0   # detector confidence when saw_pothole
    jolt_g: float | None = None


@dataclass(frozen=True)
class VerifyPolicy:
    clear_riders_needed: int = 2
    bad_riders_needed: int = 2
    strong_conf: float = 0.8
    strong_jolt_g: float = 0.5


def decide_verification(obs: list[PassObservation], policy: VerifyPolicy = VerifyPolicy()) -> Event | None:
    """ROAD_CLEAR / ROAD_STILL_BAD once enough distinct riders agree, else None (keep waiting).

    One rider with a high-confidence detection AND a strong jolt is enough to
    reopen: physical evidence beats a 'closed' label."""
    clear = {o.rider_id for o in obs if not o.saw_pothole}
    bad = {o.rider_id for o in obs if o.saw_pothole}
    strong = any(o.saw_pothole and o.model_conf >= policy.strong_conf
                 and (o.jolt_g or 0) >= policy.strong_jolt_g for o in obs)
    if strong or len(bad) >= policy.bad_riders_needed:
        return Event.ROAD_STILL_BAD
    if len(clear) >= policy.clear_riders_needed and not bad:
        return Event.ROAD_CLEAR
    return None


def sla_breached(filed_at: datetime, now: datetime, sla_days: int, escalation_level: int = 0) -> bool:
    """Each escalation level gets a fresh SLA window."""
    return now - filed_at > timedelta(days=sla_days * (escalation_level + 1))
