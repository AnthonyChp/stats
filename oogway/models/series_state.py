# oogway/models/series_state.py
# ============================================================================
# Cache mémoire d’une série Bo (aucune écriture en base)
# ============================================================================

from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from typing import List, Set, Optional


@dataclass
class Game:
    """Un match individuel à l’intérieur du Bo."""
    picks_a: List[str] = field(default_factory=list)
    picks_b: List[str] = field(default_factory=list)
    bans_a:  List[str] = field(default_factory=list)
    bans_b:  List[str] = field(default_factory=list)
    winner: Optional[str] = None             # "A" ou "B"


@dataclass
class SeriesState:
    """État global de la série — stocké uniquement en RAM."""
    id: str
    bo: int
    team_a: list[int]                        # IDs Discord
    team_b: list[int]
    captain_a: int                           # ID Discord
    captain_b: int
    blue_side: str = "A"                     # "A" ou "B"
    score_a: int = 0
    score_b: int = 0
    fearless_pool: Set[str] = field(default_factory=set)
    games: List[Game] = field(default_factory=lambda: [Game()])

    # --------------------------------------------------------------------- #
    @classmethod
    def new(cls, bo: int, team_a: list[int], team_b: list[int],
            captain_a: int, captain_b: int) -> "SeriesState":
        return cls(id=str(uuid.uuid4())[:8],
                   bo=bo,
                   team_a=team_a,
                   team_b=team_b,
                   captain_a=captain_a,
                   captain_b=captain_b)

    @property
    def current_game(self) -> Game:
        return self.games[-1]

    def start_new_game(self) -> None:
        self.games.append(Game())

    def finished(self) -> bool:
        target = self.bo // 2 + 1
        return self.score_a == target or self.score_b == target
