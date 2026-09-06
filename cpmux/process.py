# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import partial

_lease_fds: ContextVar[tuple[int, ...]] = ContextVar("cpmux_process_leases", default=())


@contextmanager
def inherit_lease(fd: int) -> Iterator[None]:
    """Include an already-held lease in this execution context's child processes.

    The caller owns the descriptor. This context does not unlock or close it.
    Subprocess adapters explicitly pass the selected descriptors across exec.

    Args:
        fd: Open descriptor carrying an operating-system file lease.

    """

    token = _lease_fds.set((*_lease_fds.get(), fd))
    try:
        yield
    finally:
        _lease_fds.reset(token)


def inherited_fds() -> tuple[int, ...]:
    """Return the lease descriptors that an owned subprocess must retain.

    Returns:
        Open descriptors selected for explicit child-process inheritance.

    """

    return _lease_fds.get()


async def complete[T](future: asyncio.Future[T]) -> T:
    """Wait for owned work without forwarding repeated cancellation into it.

    Cancellation is restored after the work finishes. A failure from the owned
    work propagates rather than being hidden by a concurrent cancellation.

    Args:
        future: Already scheduled work that must finish before ownership ends.

    Returns:
        The completed work's result when the caller was not cancelled.

    Raises:
        asyncio.CancelledError: The caller was cancelled, after the work completed.

    """

    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
    result = future.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def cancel[T](task: asyncio.Task[T]) -> None:
    """Cancel a child task and wait for its cleanup through further cancellation.

    Args:
        task: Child whose cancellation must finish before ownership is released.

    Raises:
        asyncio.CancelledError: The calling task also has a pending cancellation.

    """

    if not task.done():
        task.cancel()
    try:
        await complete(task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise


async def run_sync[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run synchronous owned work in a thread while preserving its lease context.

    The executor future is not an independently cancelled asyncio task. Caller
    cancellation waits for the operation rather than releasing its leases early.

    Args:
        function: Blocking operation whose completion must be observed.
        *args: Positional operation arguments.
        **kwargs: Keyword operation arguments.

    Returns:
        The operation result when the caller was not cancelled.

    Raises:
        asyncio.CancelledError: The caller was cancelled after the operation finished.

    """

    future = asyncio.get_running_loop().run_in_executor(
        None,
        partial(copy_context().run, function, *args, **kwargs),
    )
    return await complete(future)
