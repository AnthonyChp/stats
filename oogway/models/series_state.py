# oogway/models/series_state.py
# ============================================================================
# Cache mémoire d'une série Bo (aucune écriture en base)
# OPTIMISÉ: __slots__ pour réduire la mémoire, cache des noms capitaines
# ============================================================================

from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from typing import List, Set, Optional


@dataclass
class Game:
    """Un match individuel à l'intérieur du Bo."""
    __slots__ = ('picks_a', 'picks_b', 'bans_a', 'bans_b', 'winner')
    
    picks_a: List[str] = field(default_factory=list)
    picks_b: List[str] = field(default_factory=list)
    bans_a:  List[str] = field(default_factory=list)
    bans_b:  List[str] = field(default_factory=list)
    winner: Optional[str] = None  # "A" ou "B"


@dataclass
class SeriesState:
    """État global de la série — stocké uniquement en RAM."""
    __slots__ = (
        'id', 'bo', 'team_a', 'team_b', 'captain_a', 'captain_b',
        'blue_side', 'score_a', 'score_b', 'fearless_pool', 'games',
        'guild', 'captain_a_name', 'captain_b_name', 'status_msg_id'
    )
    
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
    
    # Cache pour optimiser les lookups répétés
    guild: Optional[object] = None
    captain_a_name: str = "Cap A"
    captain_b_name: str = "Cap B"
    status_msg_id: Optional[int] = None

    # --------------------------------------------------------------------- #
    @classmethod
    def new(cls, bo: int, team_a: list[int], team_b: list[int],
            captain_a: int, captain_b: int) -> "SeriesState":
        """Crée une nouvelle série."""
        return cls(
            id=str(uuid.uuid4())[:8],
            bo=bo,
            team_a=team_a,
            team_b=team_b,
            captain_a=captain_a,
            captain_b=captain_b,
            blue_side="A",
            score_a=0,
            score_b=0,
            fearless_pool=set(),
            games=[Game()],
            guild=None,
            captain_a_name="Cap A",
            captain_b_name="Cap B",
            status_msg_id=None
        )

    @property
    def current_game(self) -> Game:
        """Retourne la game en cours."""
        return self.games[-1]

    def start_new_game(self) -> None:
        """Démarre une nouvelle game dans la série."""
        self.games.append(Game())

    def finished(self) -> bool:
        """Vérifie si la série est terminée."""
        target = self.bo // 2 + 1
        return self.score_a >= target or self.score_b >= target
    
    def get_all_picked_champs(self) -> Set[str]:
        """Retourne tous les champions pickés dans la série (utile pour fearless)."""
        return {c for g in self.games for c in g.picks_a + g.picks_b}
    
    def swap_sides(self) -> None:
        """Inverse les sides (team A ↔ team B)."""
        self.team_a, self.team_b = self.team_b, self.team_a
        self.captain_a, self.captain_b = self.captain_b, self.captain_a
        self.captain_a_name, self.captain_b_name = self.captain_b_name, self.captain_a_name
        self.score_a, self.score_b = self.score_b, self.score_a
