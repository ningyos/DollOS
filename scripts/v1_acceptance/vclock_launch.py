"""Run the dollos daemon on a virtual wall clock (spec §9 虛擬時鐘).

  uv run --project <WT> --frozen --with time-machine==3.6.0 python vclock_launch.py --config <cfg>

env DOLLOS_VCLOCK_START  virtual epoch the driver wants at DOLLOS_VCLOCK_REAL0
    DOLLOS_VCLOCK_REAL0  real epoch when the driver launched this process
    DOLLOS_VCLOCK_OUT    JSON file: the delta measured inside this process

Starts time_machine.travel(..., tick=True) so that, in this process,
virtual = real + (START - REAL0) from the first instruction on; then calls
the console-script entry point exactly like .venv/bin/dollos does
(`from dollos.__main__ import main; sys.exit(main())`, argv[1:] untouched).
Patched: time.time/time_ns/localtime/gmtime/strftime, datetime.now/utcnow,
date.today, CLOCK_REALTIME. Not patched: time.monotonic / perf_counter (so
asyncio and timeouts are real), time.sleep, and every other process
(Shell commands, the fetch MCP server) or C library clock (SQLite).
"""
import sys

# Our own directory (the driver scripts) must not shadow anything dollos imports.
sys.path.pop(0)

import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402

import time_machine  # noqa: E402

start = float(os.environ["DOLLOS_VCLOCK_START"])
real0 = float(os.environ["DOLLOS_VCLOCK_REAL0"])
out = os.environ["DOLLOS_VCLOCK_OUT"]

time_machine.travel(start + (time.time() - real0), tick=True).start()
v = time.time()  # first read starts the tick: virtual - real is fixed from here on
r = time_machine.escape_hatch.time.time()
with open(out, "w") as f:
    json.dump({"virtual": v, "real": r, "delta": v - r, "intended_delta": start - real0,
               "pid": os.getpid(), "argv": sys.argv[1:], "tz": os.environ.get("TZ"),
               "local_virtual": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(v))}, f)

sys.argv[0] = "dollos"
from dollos.__main__ import main  # noqa: E402

sys.exit(main())
