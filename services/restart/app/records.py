"""Restart Reminder reports: what a device may send, how it updates the device's record, and how
the page describes each device.

The Windows Restart Reminder app sends a report when something happens (countdown started, the
person picked a time, hid the reminder, chose Restart now, the restart was issued, the device came
back up) and a "status" report on its regular check, so the page knows the device is still there.
Every report carries the device's last real boot time, so a restart shows up as a new boot time
even if the "restarting" report never arrived.

Rules:
* Reports are kept once each (by reportId), so a device can resend safely.
* The device record follows the newest boot. A late report from an older boot is kept in the
  history but doesn't change the record.
* When the boot time moves on, the old boot is recorded as a restart, with how long it had been up.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

EVENTS = ("status", "countdown_started", "scheduled", "hidden", "restart_now", "restarting", "booted", "dry_run")
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class Report(BaseModel):
    """One report as the device sends it. Unknown extra fields are ignored."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    reportId: str
    event: Literal["status", "countdown_started", "scheduled", "hidden", "restart_now", "restarting", "booted", "dry_run"]
    at: datetime
    deviceName: str = Field(min_length=1, max_length=64)
    serial: str = Field(default="", max_length=128)
    user: str = Field(default="", max_length=256)
    platform: Literal["windows", "macos"] = "windows"
    bootTime: datetime
    deadline: datetime | None = None          # when it will restart, if a countdown is running
    userScheduled: bool = False               # the person picked the time
    reason: str = Field(default="", max_length=200)   # for "restarting": why
    appVersion: str = Field(default="", max_length=32)
    dryRun: bool = False

    @field_validator("reportId")
    @classmethod
    def _guid(cls, v: str) -> str:
        if not GUID.match(v):
            raise ValueError("reportId must be a GUID")
        return v.lower()

    @field_validator("deadline", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v in ("", None) else v

    def device_key(self) -> str:
        return (self.serial or self.deviceName).strip().lower()

    def snapshot(self) -> dict:
        return {"reportId": self.reportId, "event": self.event, "at": iso(self.at), "deviceName": self.deviceName,
                "serial": self.serial, "user": self.user, "platform": self.platform, "bootTime": iso(self.bootTime),
                "deadline": iso(self.deadline), "userScheduled": self.userScheduled, "reason": self.reason,
                "appVersion": self.appVersion, "dryRun": self.dryRun}


def validate(raw) -> tuple[Report | None, str | None]:
    if not isinstance(raw, dict):
        return None, "each report must be an object"
    try:
        return Report.model_validate(raw), None
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "report"
        return None, f"{where}: {first.get('msg', 'invalid')}"


def same_boot(a: str | None, b: str | None) -> bool:
    """Boot times read twice can differ by a second or so."""
    da, db = parse(a), parse(b)
    return bool(da and db) and abs((da - db).total_seconds()) < 120


def merge(old: dict | None, snap: dict, *, received_at: str, key_label: str) -> tuple[dict, dict | None]:
    """(new device record, restart to record or None)."""
    base = {"deviceName": snap["deviceName"], "serial": snap["serial"], "user": snap["user"] or (old or {}).get("user", ""),
            "platform": snap["platform"], "appVersion": snap["appVersion"] or (old or {}).get("appVersion", ""),
            "keyLabel": key_label, "lastReceivedAt": received_at}
    if old is None:
        return {**base, "firstSeenAt": received_at, "bootTime": snap["bootTime"], "lastReportAt": snap["at"],
                "lastEvent": snap["event"], **_countdown(None, snap), "restarts": 0, "lastRestartAt": None,
                "postponedThisBoot": int(snap["event"] == "scheduled")}, None

    record = dict(old)
    record["lastReceivedAt"] = received_at
    restart = None
    if same_boot(snap["bootTime"], old.get("bootTime")):
        if snap["at"] >= (old.get("lastReportAt") or ""):
            record.update(base, lastReportAt=snap["at"], lastEvent=snap["event"], **_countdown(old, snap))
        if snap["event"] == "scheduled":
            record["postponedThisBoot"] = int(old.get("postponedThisBoot") or 0) + 1
    elif snap["bootTime"] > (old.get("bootTime") or ""):
        # A new boot: the device restarted.
        up = parse(snap["bootTime"]) - parse(old["bootTime"]) if old.get("bootTime") else None
        restart = {"bootTime": old.get("bootTime"), "restartedAt": snap["bootTime"],
                   "uptimeHours": round(up.total_seconds() / 3600, 1) if up else None,
                   "afterReminder": bool(old.get("countdownStartedAt")), "lastEventBefore": old.get("lastEvent"),
                   "postponed": int(old.get("postponedThisBoot") or 0)}
        record.update(base, bootTime=snap["bootTime"], lastReportAt=snap["at"], lastEvent=snap["event"],
                      **_countdown(None, snap), restarts=int(old.get("restarts") or 0) + 1,
                      lastRestartAt=snap["bootTime"], postponedThisBoot=int(snap["event"] == "scheduled"))
    return record, restart


def _countdown(old: dict | None, snap: dict) -> dict:
    """The countdown fields after this report (old is the record for the same boot, or None)."""
    old = old or {}
    running = bool(snap["deadline"]) or snap["event"] in ("restart_now", "restarting")
    return {"countdownStartedAt": (old.get("countdownStartedAt") or snap["at"]) if running else None,
            "deadline": snap["deadline"],
            "userScheduled": bool(snap["deadline"]) and snap["userScheduled"],
            "restartIssuedAt": snap["at"] if snap["event"] == "restarting" else old.get("restartIssuedAt"),
            "dryRun": snap["dryRun"]}


class Policy:
    """What counts as too long; matches the app's appsettings.json."""
    def __init__(self, max_uptime_days: float = 3, countdown_hours: float = 3, silent_hours: float = 48):
        self.max_uptime = timedelta(days=max_uptime_days)
        self.countdown = timedelta(hours=countdown_hours)
        self.silent = timedelta(hours=silent_hours)
        self.restart_grace = timedelta(minutes=30)


def status(r: dict, policy: Policy, now: datetime | None = None) -> str:
    """For the page:
    restart_failed  a restart was issued over 30 minutes ago and the device still reports the old boot
    overdue         its restart time passed over an hour ago without a restart, or (with no
                    countdown running) it's been up longer than the limit plus the countdown
    countdown       a restart is due (deadline set). A laptop that slept through the weekend can
                    be well past the limit when its countdown starts; that's not overdue.
    silent          no report for SILENT_HOURS (it may be off, or the app isn't running)
    ok              within the limit
    """
    now = now or utc_now()
    last = parse(r.get("lastReportAt")) or now
    boot = parse(r.get("bootTime")) or last
    issued = parse(r.get("restartIssuedAt"))
    if issued and last - issued > policy.restart_grace and not r.get("dryRun"):
        return "restart_failed"
    deadline = parse(r.get("deadline"))
    if deadline:
        return "overdue" if last > deadline + timedelta(hours=1) else "countdown"
    if last - boot > policy.max_uptime + policy.countdown + timedelta(hours=1):
        return "overdue"
    received = parse(r.get("lastReceivedAt")) or last
    if now - received > policy.silent:
        return "silent"
    return "ok"
