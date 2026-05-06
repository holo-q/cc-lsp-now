"""Hierarchical workgroup and project-scope discovery.

Workgroups are the social coordination layer: presence, journal, tickets, and
chat. Projects are the build/check layer. A cwd can therefore sit inside both a
domain workgroup and a buildable project. Keeping both roots visible prevents
the bus from becoming one giant mutex while still giving agents a shared room.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hsp.router import BUILTIN_ROUTES, find_project_root


WORKGROUP_MARKERS = ("workgroup.toml", ".hsp/workgroup.toml")
EXTRA_PROJECT_MARKERS = (
    "package.json",
    "pnpm-workspace.yaml",
    "go.mod",
    "justfile",
    "Justfile",
    "*.slnx",
)


@dataclass(frozen=True)
class WorkgroupDefinition:
    root: str
    marker: str
    name: str
    level: str


@dataclass(frozen=True)
class ScopeContext:
    location: str
    active_workgroup_root: str
    project_root: str
    workgroups: tuple[WorkgroupDefinition, ...]
    fallback_workgroup: bool

    @property
    def parent_workgroup_root(self) -> str:
        if len(self.workgroups) < 2:
            return ""
        return self.workgroups[-2].root


def scope_context_for(location: str | Path | None = None) -> ScopeContext:
    resolved = resolve_location(location)
    override = _explicit_workgroup_root()
    if override:
        project = discover_project_root(resolved) or str(resolved)
        return ScopeContext(
            location=str(resolved),
            active_workgroup_root=override,
            project_root=project,
            workgroups=(),
            fallback_workgroup=True,
        )
    workgroups = discover_workgroups(resolved)
    active = workgroups[-1].root if workgroups else str(resolved)
    project = discover_project_root(resolved) or str(resolved)
    return ScopeContext(
        location=str(resolved),
        active_workgroup_root=active,
        project_root=project,
        workgroups=tuple(workgroups),
        fallback_workgroup=not workgroups,
    )


def active_workgroup_root_for(location: str | Path | None = None) -> str:
    return scope_context_for(location).active_workgroup_root


def project_root_for(location: str | Path | None = None) -> str:
    return scope_context_for(location).project_root


def discover_workgroups(location: str | Path | None = None) -> list[WorkgroupDefinition]:
    resolved = resolve_location(location)
    boundary = _workgroup_boundary()
    found: list[WorkgroupDefinition] = []
    for parent in _ancestor_chain(resolved):
        marker = _workgroup_marker(parent)
        if marker is not None:
            found.append(_read_definition(parent, marker))
        if boundary and parent == boundary:
            break
    found.reverse()
    return found


def discover_project_root(location: str | Path | None = None) -> str | None:
    resolved = resolve_location(location)
    markers = _project_markers()
    return find_project_root(str(resolved), markers)


def resolve_location(location: str | Path | None = None) -> Path:
    raw = Path(os.getcwd() if location in {None, ""} else location).expanduser()
    absolute = raw if raw.is_absolute() else Path.cwd() / raw
    try:
        resolved = absolute.resolve(strict=False)
    except OSError:
        resolved = absolute.absolute()
    if resolved.exists() and resolved.is_file():
        return resolved.parent
    return resolved


def _ancestor_chain(path: Path) -> list[Path]:
    return [path, *path.parents]


def _workgroup_marker(parent: Path) -> Path | None:
    for marker in WORKGROUP_MARKERS:
        path = parent / marker
        if path.exists() and path.is_file():
            return path
    return None


def _read_definition(root: Path, marker: Path) -> WorkgroupDefinition:
    data = _read_toml(marker)
    table = data.get("workgroup", data)
    name = _string(table.get("name")) or root.name or str(root)
    level = _string(table.get("level")) or _default_level(root)
    return WorkgroupDefinition(
        root=str(root),
        marker=str(marker),
        name=name,
        level=level,
    )


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _explicit_workgroup_root() -> str:
    raw = os.environ.get("HSP_WORKGROUP_ROOT", "").strip()
    if not raw:
        return ""
    return str(resolve_location(raw))


def _workgroup_boundary() -> Path | None:
    raw = os.environ.get("HSP_WORKGROUP_BOUNDARY", "").strip()
    if not raw:
        return None
    return resolve_location(raw)


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _default_level(root: Path) -> str:
    return "domain" if root.name.startswith("repo-") else "umbrella"


def _project_markers() -> list[str]:
    markers: list[str] = []
    for route in BUILTIN_ROUTES.values():
        for marker in route.markers:
            if marker != ".git" and marker not in markers:
                markers.append(marker)
    for marker in EXTRA_PROJECT_MARKERS:
        if marker not in markers:
            markers.append(marker)
    return markers
