from __future__ import annotations
from dataclasses import dataclass, field
from cc_lsp_now.candidate_kind import CandidateKind
from cc_lsp_now.file_move import FileMove

@dataclass
class Candidate:
    kind: CandidateKind
    title: str
    edit: dict = field(default_factory=dict)
    from_path: str = ""
    to_path: str = ""
    moves: list[FileMove] = field(default_factory=list)
