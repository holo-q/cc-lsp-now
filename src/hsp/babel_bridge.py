"""Bridge Babel daemon events into the HSP workgroup bus.

Babel already owns terminal/session observation: hook lifecycles, activity
state, focus, pane open/close, and workspace placement. HSP should consume
that stream instead of inventing a parallel agent detector. This module keeps
the wire adapter pure enough to test without a live Babel daemon, while the
async subscriber can run inside the broker when enabled.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import cast

from hsp.agent_bus import AgentBus


_BABEL_EVENT_FILTERS = [
    "window_added",
    "window_removed",
    "pane_focused",
    "pane_unfocused",
    "session_matched",
    "session_updated",
    "session_state_changed",
    "activity_pulse",
    "session_started",
    "tool_started",
    "tool_completed",
    "notification_received",
    "subagent_completed",
    "transcript_compacting",
    "daemon_shutdown",
]


def babel_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "babel.sock"
    return Path("/tmp") / f"babel-{os.getuid()}.sock"


def bus_params_from_babel_frame(frame: dict[str, object]) -> dict[str, object] | None:
    if frame.get("status") != "event":
        return None
    event_obj = frame.get("event")
    if not isinstance(event_obj, dict):
        return None
    event = cast(dict[str, object], event_obj)
    native = _string(event.get("event"))
    if not native:
        return None

    metadata = _metadata_from_event(event, native)
    workspace_root = _workspace_from_event(event)
    agent_id = _agent_id(event, native)
    session_id = _session_id(event)
    addr = _pane_addr(event)
    message = _message(event, native, session_id, addr)

    params: dict[str, object] = {
        "workspace_root": workspace_root,
        "event_type": _bus_kind(native),
        "agent_id": agent_id,
        "client_id": agent_id,
        "session_id": session_id,
        "message": message,
        "metadata": metadata,
    }
    project = _string(event.get("project"))
    if project:
        params["files"] = [project]
    return params


async def subscribe_babel_events(
    bus: AgentBus,
    *,
    socket_path: Path | None = None,
    reconnect_delay: float = 5.0,
) -> None:
    """Continuously subscribe to Babel events and append them to ``bus``.

    The bridge is deliberately best-effort. If Babel is not running, HSP keeps
    serving LSP/bus traffic and retries later; the workgroup view just lacks
    extrinsic terminal signals until Babel appears.
    """
    path = socket_path or babel_socket_path()
    while True:
        try:
            await _subscribe_once(bus, path)
        except asyncio.CancelledError:
            raise
        except (OSError, json.JSONDecodeError):
            await asyncio.sleep(reconnect_delay)


async def _subscribe_once(bus: AgentBus, path: Path) -> None:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        request = {"cmd": "subscribe", "events": _BABEL_EVENT_FILTERS}
        writer.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                return
            frame = json.loads(line.decode("utf-8"))
            if not isinstance(frame, dict):
                continue
            params = bus_params_from_babel_frame(cast(dict[str, object], frame))
            if params is not None:
                bus.event(params)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def _bus_kind(native: str) -> str:
    return {
        "window_added": "agent.started",
        "terminal_became_agent": "agent.started",
        "session_started": "session.start",
        "session_matched": "session.start",
        "session_updated": "agent.heartbeat",
        "session_state_changed": "agent.heartbeat",
        "activity_pulse": "agent.heartbeat",
        "tool_started": "tool.before",
        "tool_completed": "tool.after",
        "notification_received": "notification",
        "subagent_completed": "subagent.stop",
        "transcript_compacting": "compact.before",
        "window_removed": "session.stop",
        "daemon_shutdown": "session.stop",
    }.get(native, "babel.event")


def _agent_id(event: dict[str, object], native: str) -> str:
    session_id = _session_id(event)
    kind = _string(event.get("agent_kind")) or "babel"
    if session_id:
        return f"{kind}:{session_id}"
    addr = _pane_addr(event)
    if addr:
        return f"{kind}:{addr}"
    return f"babel:{native}"


def _session_id(event: dict[str, object]) -> str:
    value = event.get("session_id")
    if isinstance(value, str):
        return value
    return ""


def _pane_addr(event: dict[str, object]) -> str:
    value = event.get("addr")
    if not isinstance(value, dict):
        return ""
    data = cast(dict[str, object], value)
    socket = _string(data.get("socket"))
    pane_id = data.get("id")
    if socket and isinstance(pane_id, int):
        return f"{socket}:{pane_id}"
    return ""


def _workspace_from_event(event: dict[str, object]) -> str:
    project = _string(event.get("project"))
    if project:
        return os.path.abspath(project)
    cwd = _string(event.get("cwd"))
    if cwd:
        return os.path.abspath(cwd)
    return os.path.abspath(os.environ.get("LSP_ROOT", os.getcwd()))


def _metadata_from_event(event: dict[str, object], native: str) -> dict[str, object]:
    metadata: dict[str, object] = {"source": "babel", "native_event": native}
    for key, value in event.items():
        if key in {"event", "timestamp"}:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            metadata[key] = "" if value is None else value
        elif isinstance(value, dict):
            metadata[key] = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return metadata


def _message(event: dict[str, object], native: str, session_id: str, addr: str) -> str:
    if native == "session_state_changed":
        old = _string(event.get("old_state"))
        new = _string(event.get("new_state"))
        return f"Babel {session_id or addr} {old}->{new}".strip()
    if native in {"tool_started", "tool_completed"}:
        return f"Babel {native} {_string(event.get('tool_name'))}".strip()
    if native == "notification_received":
        return f"Babel notification {_string(event.get('notif_type'))}".strip()
    return f"Babel {native} {session_id or addr}".strip()


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


__all__ = [
    "babel_socket_path",
    "bus_params_from_babel_frame",
    "subscribe_babel_events",
]
