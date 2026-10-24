"""Agent goals and output schemas, plus the portal registry.

Goals follow TinyFish's prompting advice: one clear task, explicit fields,
explicit stop conditions, and a typed output schema (the schema wins over
the prose). Output schemas use only the documented subset: object/array/string/
number/integer/boolean, enum (string), nullable, properties, required.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_PORTALS_PATH = Path(__file__).with_name("portals.json")


@dataclass(frozen=True)
class Portal:
    key: str
    name: str
    verified: bool
    filing_url: str
    status_mode: str            # "public_page" -> Page Monitor; "agent_lookup" -> scheduled Agent run
    status_url: str             # template with {complaint_id} for public_page
    requires_otp: bool
    category_hint: str
    escalate_to: str | None

    @property
    def usable(self) -> bool:
        return not self.filing_url.startswith("TODO")

    def status_url_for(self, complaint_id: str) -> str:
        return self.status_url.replace("{complaint_id}", complaint_id)


def load_portals(mock_base: str) -> dict[str, Portal]:
    raw = json.loads(_PORTALS_PATH.read_text(encoding="utf-8"))
    out = {}
    for key, p in raw.items():
        if key.startswith("_"):
            continue
        st = p.get("status", {})
        sub = lambda s: (s or "").replace("{MOCK}", mock_base)  # noqa: E731
        out[key] = Portal(
            key=key, name=p["name"], verified=p.get("verified", False),
            filing_url=sub(p["filing_url"]), status_mode=st.get("mode", "agent_lookup"),
            status_url=sub(st.get("url_template") or st.get("url", "")),
            requires_otp=p.get("requires_otp", False), category_hint=p.get("category_hint", "Roads"),
            escalate_to=p.get("escalate_to"),
        )
    return out


FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["filed", "needs_human", "failed"]},
        "complaint_id": {"type": "string", "nullable": True},
        "portal_message": {"type": "string", "nullable": True},
        "blocked_on": {"type": "string", "nullable": True},
        "photo_attached": {"type": "boolean"},
    },
    "required": ["status", "photo_attached"],
    "propertyOrdering": ["status", "complaint_id", "photo_attached", "portal_message", "blocked_on"],
}

STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "status_label": {"type": "string", "nullable": True},
        "remarks": {"type": "string", "nullable": True},
        "last_updated": {"type": "string", "nullable": True},
    },
    "required": ["found"],
}

AUTHORITY_SCHEMA = {
    "type": "object",
    "properties": {
        "authority": {"type": "string", "enum": ["nhai", "state_pwd", "municipal_corporation", "unknown"]},
        "authority_name": {"type": "string", "nullable": True},
        "complaint_url": {"type": "string", "nullable": True},
        "escalation_contact": {"type": "string", "nullable": True},
        "evidence_url": {"type": "string", "nullable": True},
    },
    "required": ["authority"],
}


@dataclass(frozen=True)
class Complaint:
    citizen_name: str
    citizen_phone: str
    lat: float
    lon: float
    address: str
    severity: str
    first_seen: str
    reports: int
    photo_url: str


def file_goal(portal: Portal, c: Complaint) -> str:
    return f"""You are filing a road-damage (pothole) complaint for a citizen who has explicitly approved this filing.

1. On this grievance portal, open the form to register a NEW complaint. Choose the category closest to "{portal.category_hint}".
2. Fill these fields where the form has them (skip any field the form does not have):
   - Name: {c.citizen_name}
   - Mobile: {c.citizen_phone}
   - Location / address: {c.address}
   - GPS / landmark: {c.lat:.6f}, {c.lon:.6f} (https://maps.google.com/?q={c.lat:.6f},{c.lon:.6f})
   - Description: "{c.severity.capitalize()}-severity pothole confirmed by {c.reports} independent road users since {c.first_seen}. Photo evidence: {c.photo_url} . Reported via RoadLoop."
3. If there is a photo upload field, upload the image at {c.photo_url}. If uploading is not possible, keep the photo link in the description and set photo_attached to false.
4. If the portal asks for an OTP, CAPTCHA, or a login you do not have, STOP. Do not guess. Return status "needs_human" and put the field name in blocked_on.
5. Submit the form and return the complaint / grievance / reference number EXACTLY as the portal shows it in complaint_id, with the confirmation text in portal_message.
Never submit the same complaint twice."""


def status_goal(complaint_id: str) -> str:
    return f"""Check the status of an existing complaint.
1. Open the complaint status / track grievance page of this portal.
2. Enter complaint number {complaint_id} and look it up.
3. Return the status label exactly as shown (for example Pending, Under Process, Disposed, Closed) in status_label, any officer remarks in remarks, and the last-updated date if shown.
If the complaint cannot be found, return found = false. Do not create or modify anything."""


def escalate_goal(portal: Portal, original_portal: str, complaint_id: str, c: Complaint, days_open: int,
                  reason: str) -> str:
    return f"""You are escalating an unresolved road-damage complaint for a citizen who approved this escalation.
1. On this portal, open the form for a new grievance or escalation. Choose the category closest to "{portal.category_hint}".
2. Subject: "Unresolved pothole - original complaint {complaint_id} ({original_portal})".
3. Description: "Complaint {complaint_id} filed {days_open} days ago at {c.address} (GPS {c.lat:.6f},{c.lon:.6f}). {reason} Latest photo: {c.photo_url}. Requesting repair and an inspection of the closure."
4. Name: {c.citizen_name}. Mobile: {c.citizen_phone}.
5. If an OTP, CAPTCHA or login is required, STOP and return status "needs_human" with blocked_on.
6. Submit and return the new reference number in complaint_id."""


def authority_goal(road_desc: str, city: str) -> str:
    return f"""Find which government authority is responsible for maintaining {road_desc} in {city}, India
(NHAI, the state Public Works Department, or the municipal corporation), the official online complaint page
for road damage, and an escalation contact if published. Prefer official .gov.in / .nic.in pages and cite the
page you used in evidence_url. If unclear, return authority = "unknown"."""
