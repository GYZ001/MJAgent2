"""Process-local authority set only while CommandBus executes an approved retry.

``app.api`` executes the domain chunks in its own module namespace for backwards
compatibility, while tests and command handlers may import the same chunks as
normal ``app.domain`` modules.  The authority therefore has to live in a real,
single module; defining the ContextVar inside a domain chunk creates two
independent values and makes an approved command look untrusted at the facade.
"""
from __future__ import annotations

import contextvars


SCREENPLAY_COMMAND_BUS_RETRY_APPROVAL: contextvars.ContextVar[bool] = (
    contextvars.ContextVar(
        "screenplay_command_bus_retry_approval",
        default=False,
    )
)


def enter_screenplay_command_bus_retry_approval():
    return SCREENPLAY_COMMAND_BUS_RETRY_APPROVAL.set(True)


def exit_screenplay_command_bus_retry_approval(token) -> None:
    SCREENPLAY_COMMAND_BUS_RETRY_APPROVAL.reset(token)


def consume_screenplay_command_bus_retry_approval() -> bool:
    """Consume approval exactly once before any background task is created.

    ``asyncio.create_task`` copies the caller's context.  Clearing the value in
    the activation task before ``task_registry.spawn`` prevents the privileged
    request context from leaking into the long-running screenplay worker.
    """
    approved = bool(SCREENPLAY_COMMAND_BUS_RETRY_APPROVAL.get())
    SCREENPLAY_COMMAND_BUS_RETRY_APPROVAL.set(False)
    return approved
