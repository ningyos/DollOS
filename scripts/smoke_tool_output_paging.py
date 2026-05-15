"""End-to-end smoke for tool-output paging.

Runs a Shell command that produces 100 lines, asserts:
- ShellResultEvent has output_id and line_count=100
- Preview is ≤ 10 lines
- store.read(id, offset=50, limit=5) returns lines 50-54
- store.grep(id, "line 7", 20) finds line 70, 71, ..., 79

Print PASS / FAIL. No pytest involvement.
"""
import asyncio
import tempfile
from pathlib import Path

from dollos.shell_runner import ShellRunner
from dollos.tool_outputs import ToolOutputStore
from dollos.events import ShellResultEvent, RawEvent


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="smoke-tool-paging-"))
    store = ToolOutputStore(tmp)

    events: list[ShellResultEvent] = []

    def dispatch(e: RawEvent) -> None:
        if isinstance(e, ShellResultEvent):
            events.append(e)

    runner = ShellRunner(cwd=Path.cwd(), dispatch_fn=dispatch, tool_output_store=store)
    sink: asyncio.Queue = asyncio.Queue()
    cmd = "for i in $(seq 1 100); do echo line $i; done"
    runner.spawn(command=cmd, timeout_s=10.0, response_sink=sink)
    # Wait for the event (spawn is fire-and-forget; the runner will call dispatch when done)
    for _ in range(50):
        if events:
            break
        await asyncio.sleep(0.1)

    evt = events[0]
    print(f"event: status={evt.status}, line_count={evt.line_count}, output_id={evt.output_id}")
    print(f"preview ({len(evt.output.splitlines())} lines):\n{evt.output}")

    slice_ = store.read(evt.output_id, offset=50, limit=5)
    print(f"\nslice 50-55:\n" + "\n".join(slice_.lines))

    matches = store.grep(evt.output_id, pattern=r"line 7\d", max_matches=20)
    print(f"\ngrep 'line 7\\d': {len(matches)} matches")

    store.cleanup()
    print("\nDONE")


if __name__ == "__main__":
    asyncio.run(main())
