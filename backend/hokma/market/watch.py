"""The loss of a broker connection (SPEC-016 3.8). The broker is the only source of its data, so when it stops the owner must know.

For each broker that is the source and stops (reconnecting or down), one incident:
  after SHOW_AFTER seconds, still lost: a `broker_alert` (state `lost`) goes to every panel socket: the red band;
  after NOTIFY_AFTER seconds, still lost: the owner is told once (Telegram, else mail if they turned it on), with the broker, the reason and the
  time, and never more than one loss notice per NOTICE_GAP seconds for that broker;
  when it comes back: `broker_alert` state `recovered` (the green band) and, if the loss notice went out, a recovery notice, always.
It does not look at the market hours: a broker that falls at night is told as well, and Hokma needs nothing open on screen for it.
Texts have no token, account or id.
"""
import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from hokma.i18n import t

log = logging.getLogger("hokma.market")

SHOW_AFTER = 3.0
NOTIFY_AFTER = 30.0
NOTICE_GAP = 600.0


@dataclass
class Incident:
    since: float
    code: str
    shown: bool = False
    notified: bool = False
    timers: list = field(default_factory=list)


class BrokerWatch:
    def __init__(self, hub, notify: Callable[[str], None] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.hub = hub
        self._notify = notify
        self._clock = clock
        self.incidents: dict[str, Incident] = {}
        self.last_notice: dict[str, float] = {}
        self.show_after, self.notify_after, self.notice_gap = SHOW_AFTER, NOTIFY_AFTER, NOTICE_GAP

    def set_notifier(self, notify: Callable[[str], None] | None) -> None:
        self._notify = notify

    # ---- what the hub tells ----

    def lost(self, runtime, code: str) -> None:
        loop = self.hub._loop
        if loop is None:
            return
        incident = self.incidents.get(runtime.id)
        if incident is not None:
            incident.code = code or incident.code
            return
        incident = self.incidents[runtime.id] = Incident(self._clock(), code or "network")
        incident.timers = [loop.call_later(self.show_after, self._show, runtime),
                           loop.call_later(self.notify_after, self._tell, runtime)]

    def recovered(self, runtime) -> None:
        incident = self.incidents.pop(runtime.id, None)
        if incident is None:
            return
        for timer in incident.timers:
            timer.cancel()
        if incident.shown:
            self._push({"type": "broker_alert", "state": "recovered", **self._base(runtime), "message": self._recovered_text(runtime)})
        if incident.notified:
            self._send(self._recovered_text(runtime))

    def current(self) -> list[dict]:
        """The alerts that are on screen now, for a panel that connects in the middle of an incident."""
        return [{"type": "broker_alert", "state": "lost", **self._base(self.hub._runtimes[pid]), "message": self._lost_text(self.hub._runtimes[pid])}
                for pid, incident in self.incidents.items() if incident.shown and pid in self.hub._runtimes]

    # ---- timers ----

    def _show(self, runtime) -> None:
        incident = self.incidents.get(runtime.id)
        if incident is None or incident.shown:
            return
        incident.shown = True
        self._push({"type": "broker_alert", "state": "lost", **self._base(runtime), "message": self._lost_text(runtime)})

    def _tell(self, runtime) -> None:
        incident = self.incidents.get(runtime.id)
        if incident is None or incident.notified:
            return
        now = self._clock()
        wait = self.notice_gap - (now - self.last_notice.get(runtime.id, -self.notice_gap))
        if wait > 0:
            # at most one loss notice every ten minutes per broker: the one held back is tried again when the ten minutes are over, if the loss lasts
            loop = self.hub._loop
            if loop is not None:
                incident.timers.append(loop.call_later(wait, self._tell, runtime))
            return
        incident.notified = True
        self.last_notice[runtime.id] = now
        reason = self._reason(incident.code)
        self._send(f"{self._lost_text(runtime)} {t('broker.alert.reason', reason=reason)} "
                   f"{t('broker.alert.at', time=datetime.fromtimestamp(incident.since).strftime('%H:%M'))}")

    # ---- texts and delivery ----

    @staticmethod
    def _reason(code: str) -> str:
        from hokma.market.messages import connection_message

        return connection_message(code if code.startswith("connections.") else "connections.unreachable")

    def _lost_text(self, runtime) -> str:
        return t("broker.alert.lost", name=runtime.name)

    def _recovered_text(self, runtime) -> str:
        return t("broker.alert.recovered", name=runtime.name, time=datetime.fromtimestamp(self._clock()).strftime("%H:%M"))

    def _base(self, runtime) -> dict:
        incident = self.incidents.get(runtime.id)
        return {"provider": runtime.id, "source": runtime.name, "epoch": self.hub.epoch,
                "code": incident.code if incident else "", "since": int(incident.since) if incident else None}

    def _push(self, message: dict) -> None:
        for client in list(self.hub._clients):
            client.push(message)

    def _send(self, text: str) -> None:
        notify = self._notify
        if notify is None:
            from hokma.notify.owner import notify_owner as notify
        self.hub._spawn(self._deliver(notify, text))

    @staticmethod
    async def _deliver(notify: Callable[[str], None], text: str) -> None:
        try:
            await asyncio.to_thread(notify, text)
        except Exception as error:                              # noqa: BLE001  (a notice that cannot be sent is logged, never raised)
            log.warning("broker notice not sent: %s", type(error).__name__)
