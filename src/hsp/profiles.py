from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HspProfile:
    profile_id: str
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


BUILTIN_PROFILES: dict[str, HspProfile] = {
    "python": HspProfile(
        profile_id="python",
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
    "csharp": HspProfile(
        profile_id="csharp",
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


def resolve_profile_id_for_path(file_path: str, profiles: dict[str, HspProfile] | None = None) -> str | None:
    profile_map = profiles or BUILTIN_PROFILES
    suffix = Path(file_path).suffix.lower()
    for profile in profile_map.values():
        if suffix and suffix in profile.extensions:
            return profile.profile_id

    matches: list[str] = []
    for profile in profile_map.values():
        specific_markers = tuple(marker for marker in profile.markers if marker != ".git")
        if specific_markers and find_project_root(file_path, specific_markers):
            matches.append(profile.profile_id)
    if len(matches) == 1:
        return matches[0]
    return None


def get_profile(profile_id: str) -> HspProfile | None:
    return BUILTIN_PROFILES.get(profile_id)
