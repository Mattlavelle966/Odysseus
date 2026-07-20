"""Regression tests for thread-safe access to llm_core's shared maps (issue #659).

The synchronous llm_call() runs inside FastAPI's threadpool (sync route handlers
such as POST /sessions/auto-sort), while llm_call_async() runs on the event
loop. Both mutate the module-level _response_cache / _host_fails / _dead_hosts
dicts, so those mutations must tolerate concurrent access from multiple OS
threads.

Plain thread stress can't reliably reproduce these races (CPython's GIL rarely
preempts the short critical sections), so each test deterministically widens the
vulnerable window: one injects a phantom snapshot key, the other forces every
thread to read the counter before any writes it back.
"""
import threading
import time
import asyncio

from src import llm_core


def test_cache_eviction_tolerates_already_removed_key():
    """Eviction must not raise when a snapshotted key is gone by delete time.

    Models a concurrent evictor removing the same key: the old `del` raised
    KeyError mid-loop, `pop(key, None)` does not.
    """
    class PhantomKeysCache(dict):
        def keys(self):
            # First key is absent from the dict — as if another thread evicted
            # it between the snapshot and the delete.
            return ["__phantom_removed__", *super().keys()]

    original = llm_core._response_cache
    cache = PhantomKeysCache()
    for i in range(130):  # exceed the 128 cap so the eviction branch runs
        cache[f"k{i}"] = "x"
    llm_core._response_cache = cache
    try:
        llm_core._set_cached_response("new-key", "y")  # must not raise
        assert dict.get(cache, "new-key") == "y"
    finally:
        llm_core._response_cache = original


def test_host_fail_counter_has_no_lost_updates():
    """Concurrent _mark_host_dead calls must each count exactly once.

    A SlowGetDict widens the read-modify-write window so the unguarded
    get()+1+set() loses every update but one; the lock serializes them.
    """
    url = "http://race.example:1234/v1/chat/completions"
    key = llm_core._host_key(url)

    class SlowGetDict(dict):
        def get(self, *args, **kwargs):
            value = super().get(*args, **kwargs)
            time.sleep(0.01)  # widen the gap between the read and the caller's write
            return value

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    original_fails = llm_core._host_fails
    original_threshold = llm_core._HOST_FAIL_THRESHOLD
    llm_core._host_fails = SlowGetDict()
    llm_core._HOST_FAIL_THRESHOLD = 10 ** 9  # never cool: every call is a pure +1
    try:
        def worker():
            barrier.wait()  # all threads enter the read window together
            llm_core._mark_host_dead(url)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert dict.get(llm_core._host_fails, key) == n_threads
    finally:
        llm_core._host_fails = original_fails
        llm_core._HOST_FAIL_THRESHOLD = original_threshold


def test_local_model_gate_allows_distinct_gpu_endpoints_concurrently(monkeypatch):
    monkeypatch.setattr(llm_core, "is_local_endpoint", lambda _url: True)
    llm_core._LOCAL_MODEL_LOCKS.clear()
    llm_core._LOCAL_MODEL_WAITING_FOREGROUND.clear()
    llm_core._LOCAL_MODEL_CURRENT.clear()

    async def scenario():
        both_entered = asyncio.Event()
        release = asyncio.Event()
        entered = set()

        async def worker(url):
            async with llm_core._local_model_slot(url, "ornith", "foreground"):
                entered.add(url)
                if len(entered) == 2:
                    both_entered.set()
                await release.wait()

        tasks = [
            asyncio.create_task(worker("http://host:8080/v1/chat/completions")),
            asyncio.create_task(worker("http://host:8083/v1/chat/completions")),
        ]
        await asyncio.wait_for(both_entered.wait(), timeout=0.5)
        assert len(entered) == 2
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


def test_local_model_gate_serializes_paths_on_same_endpoint(monkeypatch):
    monkeypatch.setattr(llm_core, "is_local_endpoint", lambda _url: True)
    llm_core._LOCAL_MODEL_LOCKS.clear()
    llm_core._LOCAL_MODEL_WAITING_FOREGROUND.clear()
    llm_core._LOCAL_MODEL_CURRENT.clear()

    async def scenario():
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()

        async def first():
            async with llm_core._local_model_slot(
                "http://host:8080/v1/chat/completions", "ornith-35b"
            ):
                first_entered.set()
                await release_first.wait()

        async def second():
            await first_entered.wait()
            async with llm_core._local_model_slot(
                "http://host:8080/v1/completions", "ornith-35b"
            ):
                second_entered.set()

        tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
        await first_entered.wait()
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        release_first.set()
        await asyncio.wait_for(second_entered.wait(), timeout=0.5)
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
