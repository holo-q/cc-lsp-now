from __future__ import annotations
from dataclasses import dataclass
from cc_lsp_now.candidate import Candidate

@dataclass
class PendingBuffer:
    kind: str
    candidates: list[Candidate]
    description: str
