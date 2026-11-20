"""Slows down guessing at device keys. The same file is in every service that takes device reports
(elevation, restart, notifications); a test checks the copies match.

Every wrong or missing key is logged (one JSON line: which app, from where, how many lately). An
address that sends more than LIMIT wrong keys in WINDOW seconds is turned away with 429 until its
older tries age out, whatever key it sends; other addresses aren't affected. It's kept in memory, so
a restart forgets it, which is fine: it only has to make guessing slow.

The address is the one Caddy puts in X-Forwarded-For (it replaces whatever the client sent).
"""
import json
import logging
import sys
import threading
import time

LIMIT, WINDOW = 20, 600

log = logging.getLogger("keyguard")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def client_ip(request) -> str:
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    return parts[-1] if parts else (request.client.host if request.client else "")


class KeyGuard:
    def __init__(self, app: str, limit: int = LIMIT, window: int = WINDOW):
        self.app, self.limit, self.window = app, limit, window
        self._tries: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, ip: str, now: float) -> list[float]:
        tries = [t for t in self._tries.get(ip, []) if t > now - self.window]
        if tries:
            self._tries[ip] = tries
        else:
            self._tries.pop(ip, None)
        return tries

    def blocked(self, ip: str) -> bool:
        """Whether this address has sent too many wrong keys lately."""
        with self._lock:
            return len(self._recent(ip, time.time())) >= self.limit

    def failed(self, ip: str, why: str = "bad key") -> None:
        """A wrong or missing key from this address."""
        with self._lock:
            now = time.time()
            if len(self._tries) > 10000:   # a flood from many addresses: forget the oldest
                for k in sorted(self._tries, key=lambda k: self._tries[k][-1])[:5000]:
                    del self._tries[k]
            tries = self._recent(ip, now) + [now]
            self._tries[ip] = tries
        log.info(json.dumps({"event": "bad_device_key", "app": self.app, "ip": ip, "why": why,
                             "recent": len(tries), "blocked": len(tries) >= self.limit}))

    def clear(self) -> None:
        with self._lock:
            self._tries.clear()
