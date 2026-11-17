"""Elevation records: what a device may send, and how each send is merged into the record.

The Windows worker (ElevateWorker.ps1 and Elevate60Worker.ps1) sends one record per elevation, as a
full snapshot, when it is granted or denied and again when it ends. Rules:

* Last write wins, field by field. Empty fields in a send leave the stored value alone.
* The first values of the fields that identify an elevation (user, device, serial, reason, when it
  was requested and granted) are kept for good in `first`. The worker never changes them, so if a
  later send does, the field is listed in `changed` and the log flags it. Someone with admin rights
  on the device can read the ingest key; this is how a rewritten record gets noticed.
* An elevation that has ended (expired, user_ended, revoked) or was denied is never put back to
  granted. Such a send is kept in the history and counted in `outOfOrder`.
* Every send is also kept, exactly as received, in the record's history.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

OUTCOMES = ("granted", "denied", "expired", "user_ended", "revoked")
ENDED = ("expired", "user_ended", "revoked")
TERMINAL = ("denied",) + ENDED
IDENTITY = ("user", "deviceName", "serial", "reason", "requestedAt", "grantedAt")
FIELDS = ("eventId", "serial", "deviceName", "platform", "user", "requestedAt", "grantedAt", "expiresAt",
          "durationMinutes", "reason", "outcome")
NOT_ENDED_GRACE = timedelta(minutes=20)   # a grant past its expiry by this much, with no end record
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class Elevation(BaseModel):
    """One record as the worker sends it. Unknown extra fields are ignored."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    eventId: str
    outcome: Literal["granted", "denied", "expired", "user_ended", "revoked"]
    platform: Literal["windows", "macos"] = "windows"
    user: str = Field(min_length=1, max_length=256)
    deviceName: str = Field(default="", max_length=64)
    serial: str = Field(default="", max_length=128)
    reason: str = Field(default="", max_length=2000)
    requestedAt: datetime | None = None
    grantedAt: datetime | None = None
    expiresAt: datetime | None = None
    durationMinutes: int | None = Field(default=None, ge=1, le=24 * 60)

    @field_validator("eventId")
    @classmethod
    def _guid(cls, v: str) -> str:
        if not GUID.match(v):
            raise ValueError("eventId must be a GUID")
        return v.lower()

    @field_validator("requestedAt", "grantedAt", "expiresAt", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v in ("", None) else v

    def snapshot(self) -> dict:
        """The send as stored: timestamps as UTC ISO strings, nothing else changed."""
        out = {}
        for k in FIELDS:
            v = getattr(self, k)
            out[k] = iso(v if v.tzinfo else v.replace(tzinfo=timezone.utc)) if isinstance(v, datetime) else v
        return out


def validate(raw) -> tuple[Elevation | None, str | None]:
    """(record, None) or (None, why it was refused)."""
    if not isinstance(raw, dict):
        return None, "each event must be an object"
    try:
        return Elevation.model_validate(raw), None
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "record"
        return None, f"{where}: {first.get('msg', 'invalid')}"


def merge(old: dict | None, ev: Elevation, *, received_at: str, key_label: str, source_ip: str) -> tuple[dict, dict]:
    snap = ev.snapshot()
    entry = {**snap, "receivedAt": received_at, "keyLabel": key_label, "sourceIp": source_ip}

    if old is None:
        record = {k: v for k, v in snap.items()}
        record.update(first={k: snap.get(k) for k in IDENTITY}, firstKeyLabel=key_label, changed=[],
                      outOfOrder=0, sends=1, firstReceivedAt=received_at, lastReceivedAt=received_at,
                      keyLabel=key_label)
        return record, {**entry, "seq": 1}

    record = dict(old)
    changed = set(record.get("changed") or [])
    first = dict(record.get("first") or {})

    regress = old.get("outcome") in TERMINAL and snap["outcome"] == "granted"
    for k, v in snap.items():
        if v in (None, "") or k == "eventId":
            continue
        if regress and k in ("outcome", "expiresAt"):
            continue
        if k in IDENTITY and first.get(k) not in (None, "") and first.get(k) != v:
            changed.add(k)
        if k in IDENTITY and first.get(k) in (None, ""):
            first[k] = v   # e.g. grantedAt, unknown on a record first seen as denied
        record[k] = v
    if key_label != record.get("firstKeyLabel"):
        changed.add("key")

    record.update(first=first, changed=sorted(changed), outOfOrder=int(old.get("outOfOrder") or 0) + regress,
                  sends=int(old.get("sends") or 0) + 1, lastReceivedAt=received_at, keyLabel=key_label)
    return record, {**entry, "seq": record["sends"], "ignoredAsOutOfOrder": bool(regress)}


def status(r: dict, now: datetime | None = None) -> str:
    """For display: active, not_ended (granted, past expiry, no end record), or the outcome."""
    if r.get("outcome") != "granted":
        return r.get("outcome") or "unknown"
    now = now or utc_now()
    exp = parse(r.get("expiresAt"))
    if exp and now > exp + NOT_ENDED_GRACE:
        return "not_ended"
    return "active"
