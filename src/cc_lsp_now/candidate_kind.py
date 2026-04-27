from __future__ import annotations
from enum import Enum

class CandidateKind(Enum):
    CODE_ACTION = "code_action"
    SYMBOL_RENAME = "symbol_rename"
    FILE_MOVE = "file_move"
    FILE_MOVE_BATCH = "file_move_batch"
    FILE_CREATE = "file_create"
    FILE_DELETE = "file_delete"
