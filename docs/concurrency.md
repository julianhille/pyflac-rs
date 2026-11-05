<!-- SPDX-License-Identifier: 0BSD -->
# Async, threads and the GIL

## The GIL is released

Every function releases the GIL for the whole encode, decode or remux. Calls
from several Python threads therefore run in parallel on several cores:

```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor() as pool:
    files = list(pool.map(lambda pcm: pyflac_rs.encode(pcm, **fmt), chunks))
```

The input is copied before the GIL is released, so no Python object is read
while other threads run.

## Async

Each function has an `*_async` twin that runs the same call in an executor:
the event loop's default thread pool, or the `executor=` you pass. The loop
stays responsive, and several calls run at the same time:

```python
flac, ogg = await asyncio.gather(
    pyflac_rs.encode_async(pcm, **fmt),
    pyflac_rs.remux_async(other, "ogg"),
)
```

Cancelling an awaiting task does not stop a call that already started. It
runs to the end in its thread, and the result is discarded.

This works the same with [uvloop](https://pypi.org/project/uvloop/):

```python
import uvloop

uvloop.run(main())
```

`tests/test_event_loop.py` checks this on asyncio's own loop and on uvloop. A
heartbeat ticks every millisecond while a long encode or decode runs. The
test fails if the heartbeat stops for half of the call or longer, which
is what a call that held the GIL would cause. The same check runs for the
sync functions with the heartbeat in another thread.

A process pool gives no speed-up over threads here and adds pickling of the
samples. Use threads.

## Free-threaded Python

The extension declares itself free-threading safe (`gil_used = false`), and
free-threaded wheels (`cp314t`, `cp315t`) are published next to the abi3
wheel. Importing pyflac-rs on a free-threaded interpreter does not turn the
GIL back on. CI runs the test suite on 3.14t and 3.15t.
