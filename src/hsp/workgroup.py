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
    observation_mode: str = "subtree"
    observation_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScopeContext:
    location: str
    active_workgroup_root: str
    project_root: str
    workgroups: tuple[WorkgroupDefinition, ...]
    fallback_workgroup: bool
    workgroup_source: str

    @property
    def parent_workgroup_root(self) -> str:
        if len(self.workgroups) < 2:
            return ""
        return self.workgroups[-2].root

    @property
    def active_workgroup(self) -> WorkgroupDefinition | None:
        return self.workgroups[-1] if self.workgroups else None

    @property
    def observation_mode(self) -> str:
        active = self.active_workgroup
        return active.observation_mode if active is not None else "exact"

    @property
    def observation_roots(self) -> tuple[str, ...]:
        active = self.active_workgroup
        if active is None:
            return (self.active_workgroup_root,)
        return tuple(dict.fromkeys((active.root, *active.observation_roots)))


def scope_context_for(location: str | Path | None = None) -> ScopeContext:
    resolved = resolve_location(location)
    override = _explicit_workgroup_root()
    if override:
        project = discover_project_root(resolved, boundary=Path(override)) or override
        return ScopeContext(
            location=str(resolved),
            active_workgroup_root=override,
            project_root=project,
            workgroups=(),
            fallback_workgroup=True,
            workgroup_source="override",
        )
    workgroups = discover_workgroups(resolved)
    active = workgroups[-1].root if workgroups else str(resolved)
    project = discover_project_root(resolved, boundary=Path(active)) or active
    return ScopeContext(
        location=str(resolved),
        active_workgroup_root=active,
        project_root=project,
        workgroups=tuple(workgroups),
        fallback_workgroup=not workgroups,
        workgroup_source="fallback" if not workgroups else "marker",
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


def discover_project_root(location: str | Path | None = None, *, boundary: Path | None = None) -> str | None:
    resolved = resolve_location(location)
    markers = _project_markers()
    if boundary is None:
        return find_project_root(str(resolved), markers)
    return _find_project_root_until(resolved, markers, boundary=boundary)


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


def _find_project_root_until(path: Path, markers: list[str], *, boundary: Path) -> str | None:
    boundary = boundary.resolve(strict=False)
    start = path if path.is_dir() else path.parent
    for parent in _ancestor_chain(start):
        for marker in markers:
            if _has_marker(parent, marker):
                return str(parent)
        if parent == boundary:
            break
    return None


def _has_marker(parent: Path, marker: str) -> bool:
    if any(ch in marker for ch in "*?["):
        try:
            return any(parent.glob(marker))
        except OSError:
            return False
    return (parent / marker).exists()


def _workgroup_marker(parent: Path) -> Path | None:
    for marker in WORKGROUP_MARKERS:
        path = parent / marker
        if path.exists() and path.is_file():
            return path
    return None


def _read_definition(root: Path, marker: Path) -> WorkgroupDefinition:
    data = _read_toml(marker)
    table = data.get("workgroup", data)
    observe = data.get("observe", {})
    observe_table = observe if isinstance(observe, dict) else {}
    name = _string(table.get("name")) or root.name or str(root)
    level = _string(table.get("level")) or _default_level(root)
    observation_mode = _observation_mode(table, observe_table)
    return WorkgroupDefinition(
        root=str(root),
        marker=str(marker),
        name=name,
        level=level,
        observation_mode=observation_mode,
        observation_roots=tuple(_observation_roots(root, table, observe_table)),
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


def _observation_mode(table: dict[str, Any], observe: dict[str, Any]) -> str:
    raw = (
        _string(observe.get("mode"))
        or _string(table.get("observe"))
        or _string(table.get("observation"))
        or "subtree"
    ).lower()
    if raw in {"exact", "self"}:
        return "exact"
    if raw in {"network", "roots", "explicit"}:
        return "network"
    return "subtree"


def _observation_roots(root: Path, table: dict[str, Any], observe: dict[str, Any]) -> list[str]:
    raw = (
        observe.get("roots")
        or table.get("observe_roots")
        or table.get("observation_roots")
        or []
    )
    roots: list[str] = []
    for item in _string_list(raw):
        path = Path(item).expanduser()
        absolute = path if path.is_absolute() else root / path
        roots.append(str(absolute.resolve(strict=False)))
    return roots


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


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
