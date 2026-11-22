"""SPEC-016 T18: the loss of the broker that is the source. Banner after 3 seconds, one notice to the owner after 30, never more than one every ten
minutes, a recovery notice, with the market closed too, and nothing secret in the text. The timers are shortened; the notifier is a list."""
import asyncio

import pytest

from hokma.market.stream import SourceStatus
from test_hub_streaming import message, run
from test_source_selection import Config, FakeProvider, hub_of, providers  # noqa: F401  (fixture)
from hokma.market.hub import PanelClient
from test_source_selection import chart


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "en")


def watched(hub, sent: list):
    hub.watch.set_notifier(sent.append)
    hub.watch.show_after, hub.watch.notify_after, hub.watch.notice_gap = 0.05, 0.15, 600.0
    return hub


async def until(condition, timeout: float = 5.0) -> None:
    """Waits for what the hub did, never for an amount of time: a slow machine only takes longer."""
    end = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < end, "the hub did not get there"
        await asyncio.sleep(0.005)


async def started(providers, sent):
    hub = watched(hub_of(Config()), sent)
    client = PanelClient()
    hub.attach_client(client)
    await chart(hub, client, "a")
    return hub, client


def test_the_band_comes_after_3_seconds_the_notice_after_30_and_the_recovery_is_told(providers):
    sent: list = []

    async def go():
        hub, client = await started(providers, sent)
        providers["schwab"].emit(SourceStatus("reconnecting", "network"))
        lost = await message(client, lambda m: m["type"] == "broker_alert" and m["state"] == "lost")
        assert lost["source"] == "Schwab" and lost["message"] == "Connection with Schwab was lost. Data is not updating."
        assert sent == []                                              # the band first, the notice later
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.02)
        assert len(sent) == 1 and "Schwab" in sent[0] and "Reason:" in sent[0]
        providers["schwab"].emit(SourceStatus("live", "connected"))
        green = await message(client, lambda m: m["type"] == "broker_alert" and m["state"] == "recovered")
        assert "recovered at" in green["message"]
        for _ in range(100):
            if len(sent) == 2:
                break
            await asyncio.sleep(0.02)
        assert len(sent) == 2 and "recovered at" in sent[1]
        await hub.close()
    run(go())


def test_a_drop_that_comes_back_before_the_band_makes_no_noise(providers):
    sent: list = []

    async def go():
        hub, client = await started(providers, sent)
        hub.watch.show_after = hub.watch.notify_after = 60.0          # the band cannot come inside the test, however slow the machine is
        providers["schwab"].emit(SourceStatus("reconnecting", "network"))
        await until(lambda: "schwab" in hub.watch.incidents)
        providers["schwab"].emit(SourceStatus("live", "connected"))
        await until(lambda: "schwab" not in hub.watch.incidents)         # the loss was seen and the recovery too, before any band
        assert sent == [] and not [m for m in drain(client) if m["type"] == "broker_alert"]
        await hub.close()
    run(go())


def drain(client) -> list:
    import json

    out = []
    while not client.outbox.empty():
        out.append(json.loads(client.outbox.get_nowait()))
    return out


def test_never_more_than_one_loss_notice_every_ten_minutes_for_a_broker(providers):
    sent: list = []

    async def go():
        hub, client = await started(providers, sent)
        now = [1000.0]
        hub.watch._clock = lambda: now[0]                                # the ten minutes run on a clock the test owns
        providers["schwab"].emit(SourceStatus("reconnecting", "network"))
        await until(lambda: any("was lost" in text for text in sent))
        providers["schwab"].emit(SourceStatus("live", "connected"))
        await until(lambda: "schwab" not in hub.watch.incidents)
        providers["schwab"].emit(SourceStatus("reconnecting", "network"))
        await until(lambda: "schwab" in hub.watch.incidents and len(hub.watch.incidents["schwab"].timers) >= 3)   # the notice was tried and held back
        losses = [text for text in sent if "was lost" in text]
        assert len(losses) == 1                                         # the second incident came inside the ten minutes
        await hub.close()
    run(go())


def test_the_notice_held_back_by_the_ten_minute_gap_is_sent_when_the_gap_is_over_if_the_loss_lasts(providers):
    sent: list = []

    async def go():
        hub, client = await started(providers, sent)
        now = [1000.0]
        hub.watch._clock = lambda: now[0]                                          # the gap runs on a clock the test owns
        hub.watch.notice_gap = 0.5
        providers["schwab"].emit(SourceStatus("reconnecting", "network"))
        await until(lambda: any("was lost" in text for text in sent))
        providers["schwab"].emit(SourceStatus("live", "connected"))
        await until(lambda: "schwab" not in hub.watch.incidents)
        providers["schwab"].emit(SourceStatus("reconnecting", "network"))        # a second loss inside the gap: held back
        await until(lambda: "schwab" in hub.watch.incidents and len(hub.watch.incidents["schwab"].timers) >= 3)
        assert len([t for t in sent if "was lost" in t]) == 1                     # the clock has not moved: the gap cannot be over
        now[0] += 1.0                                                             # the gap is over; the loss lasts: it is told
        await until(lambda: len([t for t in sent if "was lost" in t]) == 2)
        await hub.close()
    run(go())


def test_a_panel_that_connects_in_the_middle_of_the_incident_gets_the_band(providers):
    sent: list = []

    async def go():
        hub, client = await started(providers, sent)
        providers["schwab"].emit(SourceStatus("reconnecting", "network"))
        await message(client, lambda m: m["type"] == "broker_alert")
        late = PanelClient()
        hub.attach_client(late)
        assert (await message(late, lambda m: m["type"] == "broker_alert"))["state"] == "lost"
        await hub.close()
    run(go())


def test_the_text_has_the_broker_the_reason_and_the_time_and_no_secret(providers):
    sent: list = []

    async def go():
        hub, client = await started(providers, sent)
        providers["schwab"].emit(SourceStatus("down", "connections.schwab_expired"))
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.02)
        text = sent[0]
        assert "Schwab" in text and "Since " in text and "Reason:" in text
        assert "token" not in text.lower() and "Bearer" not in text
        await hub.close()
    run(go())
