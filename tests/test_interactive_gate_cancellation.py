"""Cancellation safety for the foreground/background activity gate."""

import asyncio

import pytest

import src.interactive_gate as gate


@pytest.fixture(autouse=True)
def reset_gate(monkeypatch):
    monkeypatch.setenv("BACKGROUND_TASK_FOREGROUND_GATE", "true")
    monkeypatch.setenv("BACKGROUND_TASK_QUIET_MS", "25")
    monkeypatch.setenv("BACKGROUND_TASK_BROWSER_ACTIVE_SECONDS", "45")
    gate._ACTIVE_REQUESTS = 0
    gate._LAST_ACTIVITY = 0.0
    gate._LAST_BROWSER_ACTIVITY = 0.0
    gate._COND = None
    gate._COND_LOOP = None
    yield
    gate._ACTIVE_REQUESTS = 0
    gate._LAST_ACTIVITY = 0.0
    gate._LAST_BROWSER_ACTIVITY = 0.0
    gate._COND = None
    gate._COND_LOOP = None


@pytest.mark.asyncio
async def test_cancelled_quiet_wait_does_not_lock_future_requests():
    await gate.mark_browser_activity()
    waiter = asyncio.create_task(
        gate.wait_for_interactive_quiet("scheduled task")
    )
    await asyncio.sleep(0.02)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    cond = gate._condition()
    assert not cond.locked()

    async def one_request():
        async with gate.track_interactive_request("/api/version", "GET"):
            return "ok"

    assert await asyncio.wait_for(one_request(), timeout=0.5) == "ok"
    await asyncio.wait_for(gate.mark_browser_activity(), timeout=0.5)


@pytest.mark.asyncio
async def test_many_cancelled_waiters_leave_activity_gate_usable():
    await gate.mark_browser_activity()
    waiters = [
        asyncio.create_task(gate.wait_for_interactive_quiet(f"task-{index}"))
        for index in range(20)
    ]
    await asyncio.sleep(0.02)

    for waiter in waiters:
        waiter.cancel()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert not gate._condition().locked()
    await asyncio.wait_for(gate.mark_browser_activity(), timeout=0.5)


def test_agentide_status_poll_is_passive_but_mutations_are_foreground():
    assert not gate.should_track_interactive_request(
        "/api/agentide/sessions/site-a",
        "GET",
    )
    assert not gate.should_track_interactive_request(
        "/api/agentide/sessions",
        "HEAD",
    )
    assert gate.should_track_interactive_request(
        "/api/agentide/sessions",
        "POST",
    )
    assert gate.should_track_interactive_request(
        "/api/agentide/sessions/site-a",
        "DELETE",
    )


def test_read_only_panel_polls_are_passive_but_note_mutations_are_foreground():
    assert not gate.should_track_interactive_request("/api/notes", "GET")
    assert not gate.should_track_interactive_request(
        "/api/calendar/events", "GET"
    )
    assert not gate.should_track_interactive_request(
        "/api/activity/heartbeat", "POST"
    )
    assert gate.should_track_interactive_request("/api/notes", "POST")
