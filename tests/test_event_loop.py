"""The GIL is released for the whole encode or decode: the async functions never
stall the event loop (asyncio's own loop and uvloop), and the sync functions
never stall other Python threads.

A heartbeat ticks every millisecond while a long call runs. If the call held
the GIL, the heartbeat would stop for the whole call. It may pause for the
short attached parts (copying the input in and the result out), and on busy
CI runners the OS may not schedule it for a while, so a pause must only stay
well short of the call."""

import asyncio
import os
import sys
import threading
import time

import numpy as np
import pytest

import pyflac_rs

FMT = {"channels": 2, "bits_per_sample": 16, "sample_rate": 44_100}
TICK = 0.001
# The call must take long enough that a stalled heartbeat would be obvious.
MIN_CALL = 0.1
# Leave a CPU for the heartbeat: more busy threads than CPUs starve it even
# with the GIL released.
CPUS = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 2
JOBS = max(2, CPUS - 1)


@pytest.fixture(scope="module")
def media():
    # 90 s of stereo noise: noise compresses badly, so it is slow to encode.
    rng = np.random.default_rng(7)
    pcm = rng.integers(-20_000, 20_000, (90 * 44_100, 2)).astype(np.int16)
    flac = pyflac_rs.encode(pcm, seek_points=10, **FMT)
    return {
        "pcm": pcm,
        "flac": flac,
        "ogg": pyflac_rs.remux(flac, "ogg"),
        "frames": pyflac_rs.remux(flac, "frames"),
    }


# (name, function, input, keyword options). The slow ones are timed; remux
# and read_metadata do not decode, so they are fast, but they must not block
# either.
CALLS = [
    ("encode", "pcm", FMT),
    ("encode_ogg", "pcm", FMT),
    ("encode_frames", "pcm", FMT),
    ("decode", "flac", {}),
    ("decode_ogg", "ogg", {}),
    ("decode_frames", "frames", {}),
    ("decode_seek", "flac", {"target_sample": 44_100}),
]
FAST = [
    ("remux", "flac", {"to": "ogg"}),
    ("read_metadata", "ogg", {}),
]


def loops():
    if sys.platform == "win32":
        uv = pytest.param(None, id="uvloop", marks=pytest.mark.skip("no uvloop on Windows"))
    else:
        # A dev dependency everywhere but Windows: a missing uvloop fails.
        import uvloop

        uv = pytest.param(uvloop.new_event_loop, id="uvloop")
    return [pytest.param(asyncio.new_event_loop, id="asyncio"), uv]


def check_gaps(stamps, started, elapsed, timed):
    """Fail if the heartbeat stalled for a large part of a timed call."""
    gaps = np.diff([started, *stamps])
    assert len(gaps), "the heartbeat never ran during the call"
    if timed:
        assert elapsed >= MIN_CALL, f"call took {elapsed:.3f} s, too short to judge"
        # Held GIL: one gap as long as the call. Released: shorter pauses.
        assert gaps.max() < elapsed / 2, (
            f"heartbeat stalled {gaps.max() * 1000:.1f} ms during a {elapsed * 1000:.1f} ms call"
        )
        assert len(gaps) >= elapsed / (TICK * 20)


async def heartbeat_during(coro):
    stamps = []
    done = False

    async def beat():
        while not done:
            await asyncio.sleep(TICK)
            stamps.append(time.perf_counter())

    task = asyncio.create_task(beat())
    await asyncio.sleep(0.02)  # let the heartbeat settle
    stamps.clear()
    started = time.perf_counter()
    result = await coro
    elapsed = time.perf_counter() - started
    done = True
    await task
    return result, [s for s in stamps if s <= started + elapsed], started, elapsed


@pytest.mark.parametrize("new_loop", loops())
@pytest.mark.parametrize(
    ("name", "source", "options"), CALLS + FAST, ids=lambda v: v if isinstance(v, str) else ""
)
def test_async_call_never_blocks_the_loop(new_loop, name, source, options, media):
    fn = getattr(pyflac_rs, f"{name}_async")
    loop = new_loop()
    try:
        _, stamps, started, elapsed = loop.run_until_complete(
            heartbeat_during(fn(media[source], **options))
        )
    finally:
        loop.close()
    check_gaps(stamps, started, elapsed, timed=(name, source, options) in CALLS)


@pytest.mark.parametrize("new_loop", loops())
def test_many_concurrent_calls_keep_the_loop_responsive(new_loop, media):
    async def all_at_once():
        jobs = [pyflac_rs.decode_async(media["flac"]) for _ in range(JOBS - JOBS // 2)]
        jobs += [pyflac_rs.encode_async(media["pcm"], **FMT) for _ in range(JOBS // 2)]
        return await asyncio.gather(*jobs)

    loop = new_loop()
    try:
        results, stamps, started, elapsed = loop.run_until_complete(heartbeat_during(all_at_once()))
    finally:
        loop.close()
    check_gaps(stamps, started, elapsed, timed=True)
    decoded, encoded = results[: JOBS - JOBS // 2], results[JOBS - JOBS // 2 :]
    for audio in decoded:
        np.testing.assert_array_equal(audio.to_numpy(), media["pcm"])
    assert len(set(encoded)) == 1


@pytest.mark.parametrize(
    ("name", "source", "options"), CALLS + FAST, ids=lambda v: v if isinstance(v, str) else ""
)
def test_sync_call_never_blocks_other_threads(name, source, options, media):
    fn = getattr(pyflac_rs, name)
    stamps = []
    stop = threading.Event()

    def beat():
        while not stop.is_set():
            time.sleep(TICK)
            stamps.append(time.perf_counter())

    thread = threading.Thread(target=beat)
    thread.start()
    try:
        time.sleep(0.02)
        started = time.perf_counter()
        fn(media[source], **options)
        elapsed = time.perf_counter() - started
    finally:
        stop.set()
        thread.join()
    stamps = [s for s in stamps if started < s <= started + elapsed]
    check_gaps(stamps, started, elapsed, timed=(name, source, options) in CALLS)
