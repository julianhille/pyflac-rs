"""Many threads and tasks at once: results must not mix, whether the GIL is
released (regular CPython) or absent (free-threaded 3.14t/3.15t)."""

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from test_codec import FMT, sine

import pyflac_rs
from pyflac_rs import Metadata

THREADS = 8


def job(i: int) -> None:
    # Each thread uses its own signal, so mixed-up results cannot pass.
    pcm = np.roll(sine(20_000 + 997 * i, 2, 16), i * 31, axis=0)
    meta = Metadata.build(tags={"TITLE": f"track {i}"})
    flac = pyflac_rs.encode(pcm, metadata=meta, seek_points=4, **FMT)
    np.testing.assert_array_equal(pyflac_rs.decode(flac).to_numpy(), pcm)
    ogg = pyflac_rs.remux(flac, "ogg", serial=i)
    info = pyflac_rs.read_metadata(ogg)
    assert info.serial == i and info.metadata.get("title") == [f"track {i}"]
    np.testing.assert_array_equal(pyflac_rs.decode_ogg(ogg).to_numpy(), pcm)
    seek = pyflac_rs.decode_seek(flac, 10_000)
    np.testing.assert_array_equal(seek.to_numpy(), pcm[seek.first_sample :])


@pytest.mark.parametrize("rounds", [1, pytest.param(20, marks=pytest.mark.full)])
def test_threads_do_not_mix_results(rounds):
    with ThreadPoolExecutor(THREADS) as pool:
        list(pool.map(job, range(THREADS * rounds)))


def test_shared_input_buffer_is_safe():
    # One numpy array read by every thread at once.
    pcm = sine(100_000, 2, 16)
    expected = pyflac_rs.encode(pcm, **FMT)
    with ThreadPoolExecutor(THREADS) as pool:
        outs = list(pool.map(lambda _: pyflac_rs.encode(pcm, **FMT), range(THREADS * 2)))
    assert set(outs) == {expected}


def test_async_with_explicit_executor():
    pcm = sine(30_000, 2, 16)

    async def main(pool):
        flacs = await asyncio.gather(
            *(
                pyflac_rs.encode_async(pcm, executor=pool, compression_level=lv, **FMT)
                for lv in range(9)
            )
        )
        decoded = await asyncio.gather(*(pyflac_rs.decode_async(f, executor=pool) for f in flacs))
        for d in decoded:
            np.testing.assert_array_equal(d.to_numpy(), pcm)
        ogg = await pyflac_rs.remux_async(flacs[5], "ogg", executor=pool)
        info = await pyflac_rs.read_metadata_async(ogg, executor=pool)
        assert info.container == "ogg"

    with ThreadPoolExecutor(3) as pool:
        asyncio.run(main(pool))


def test_free_threaded_build_keeps_the_gil_off():
    is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
    if is_gil_enabled is None:
        pytest.skip("not a free-threaded-capable CPython")
    import sysconfig

    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        pytest.skip("regular (GIL) build")
    # Importing the extension must not re-enable the GIL (gil_used = false).
    assert not is_gil_enabled()
