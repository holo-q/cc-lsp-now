from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LanguageRoute:
    route_id: str
    language: str
    display_name: str
    extensions: tuple[str, ...]
    markers: tuple[str, ...]
    env: dict[str, str]


PYTHON_PREFER = (
    "workspace/willRenameFiles=basedpyright-langserver,"
    "textDocument/prepareCallHierarchy=basedpyright-langserver,"
    "callHierarchy/incomingCalls=basedpyright-langserver,"
    "callHierarchy/outgoingCalls=basedpyright-langserver"
)


BUILTIN_ROUTES: dict[str, LanguageRoute] = {
    "python": LanguageRoute(
        route_id="python",
        language="python",
        display_name="Python",
        extensions=(".py", ".pyi"),
        markers=("pyproject.toml", "setup.py", "setup.cfg", ".git"),
        env={
            "LSP_SERVERS": "ty server;basedpyright-langserver --stdio",
            "LSP_PREFER": PYTHON_PREFER,
            "LSP_PROJECT_MARKERS": "pyproject.toml,setup.py,setup.cfg,.git",
            "LSP_WARMUP_PATTERNS": "*.py,*.pyi",
            "LSP_WARMUP_EXCLUDE": "references,tmp,dist,build",
            "LSP_LANGUAGE": "python",
        },
    ),
    "csharp": LanguageRoute(
        route_id="csharp",
        language="csharp",
        display_name="C#",
        extensions=(".cs",),
        markers=("*.sln", "*.csproj", "Directory.Build.props", "global.json", ".git"),
        env={
            "LSP_SERVERS": "csharp-ls",
            "LSP_PROJECT_MARKERS": "*.sln,*.csproj,Directory.Build.props,global.json,.git",
            "LSP_WARMUP_PATTERNS": "*.cs",
            "LSP_WARMUP_EXCLUDE": "bin,obj,packages,.vs,node_modules",
            "LSP_LANGUAGE": "csharp",
        },
    ),
    "rust": LanguageRoute(
        route_id="rust",
        language="rust",
        display_name="Rust",
        extensions=(".rs",),
        markers=("Cargo.toml", "rust-project.json", ".git"),
        env={
            "LSP_SERVERS": "rust-analyzer",
            "LSP_PROJECT_MARKERS": "Cargo.toml,rust-project.json,.git",
            "LSP_WARMUP_PATTERNS": "*.rs",
            "LSP_WARMUP_EXCLUDE": "target,references,tmp,.git",
            "LSP_LANGUAGE": "rust",
        },
    ),
}


def has_marker(parent: Path, marker: str) -> bool:
    if any(ch in marker for ch in "*?["):
        try:
            return any(parent.glob(marker))
        except OSError:
            return False
    return (parent / marker).exists()


def find_project_root(file_path: str, markers: list[str] | tuple[str, ...]) -> str | None:
    if not markers:
        return None
    path = Path(file_path).resolve()
    start = path if path.is_dir() else path.parent
    for parent in [start, *start.parents]:
        for marker in markers:
            if has_marker(parent, marker):
                return str(parent)
    return None


def resolve_route_id_for_path(file_path: str, routes: dict[str, LanguageRoute] | None = None) -> str | None:
    route_map = routes or BUILTIN_ROUTES
    suffix = Path(file_path).suffix.lower()
    for route in route_map.values():
        if suffix and suffix in route.extensions:
            return route.route_id

    matches: list[tuple[str, Path]] = []
    for route in route_map.values():
        specific_markers = tuple(marker for marker in route.markers if marker != ".git")
        root = find_project_root(file_path, specific_markers)
        if specific_markers and root:
            matches.append((route.route_id, Path(root)))
    if len(matches) == 1:
        return matches[0][0]
    if matches:
        deepest = max(len(root.parts) for _route_id, root in matches)
        nearest = [route_id for route_id, root in matches if len(root.parts) == deepest]
        if len(nearest) == 1:
            return nearest[0]
    return None


def get_route(route_id: str) -> LanguageRoute | None:
    return BUILTIN_ROUTES.get(route_id)
