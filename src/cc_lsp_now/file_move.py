from __future__ import annotations
from dataclasses import dataclass

@dataclass
class FileMove:
    from_path: str
    to_path: str
