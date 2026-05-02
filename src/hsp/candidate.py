from __future__ import annotations
from dataclasses import dataclass, field
from hsp.candidate_kind import CandidateKind
from hsp.file_move import FileMove

@dataclass
class Candidate:
    kind: CandidateKind
    title: str
    edit: dict = field(default_factory=dict)
    from_path: str = ""
    to_path: str = ""
    moves: list[FileMove] = field(default_factory=list)
