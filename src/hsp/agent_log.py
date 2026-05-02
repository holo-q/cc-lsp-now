"""Custom AGENT logging level — messages surface in MCP tool output.

Usage anywhere in hsp:

    from hsp.agent_log import agent_log, drain_agent_messages

    agent_log("Pylance says: no files in program")  # surfaces in tool output
    log.info("internal detail")                      # stays in Python logs

The _wrap_with_header wrapper calls drain_agent_messages() each tool
call and prepends them to the output so the model sees them inline.
"""
from __future__ import annotations

import logging

AGENT = 25
logging.addLevelName(AGENT, "AGENT")


class _AgentHandler(logging.Handler):
    """Buffers AGENT-level messages for draining into MCP tool output."""

    def __init__(self) -> None:
        super().__init__(level=AGENT)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno == AGENT:
            self.messages.append(self.format(record))


_handler = _AgentHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger("hsp").addHandler(_handler)


def agent_log(msg: str) -> None:
    """Log a message that will appear in the next MCP tool response."""
    logging.getLogger("hsp").log(AGENT, msg)


def drain_agent_messages() -> list[str]:
    """Return and clear all buffered AGENT messages."""
    msgs = list(_handler.messages)
    _handler.messages.clear()
    return msgs
