from __future__ import annotations
from dataclasses import dataclass

@dataclass
class ChainServer:
    command: str
    args: list[str]
    name: str
    label: str
