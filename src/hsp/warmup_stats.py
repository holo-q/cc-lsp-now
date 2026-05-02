from __future__ import annotations
from dataclasses import dataclass

@dataclass
class WarmupStats:
    count: int
    timestamp: float
