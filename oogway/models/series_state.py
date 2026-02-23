# oogway/models/series_state.py
# ============================================================================
# Cache mémoire d'une série Bo (aucune écriture en base)
# OPTIMISÉ: __slots__ pour réduire la mémoire, cache des noms capitaines
# + Remplacement joueurs (substitute)
# + Sérialisation pour historique Redis (to_history_dict / from_history_dict)
# ============================================================================
from __future__ import annotations

import random
import time
import uuid
from typing import Any, Dict, List, Optional, Set


class Game:
    """Un match individuel à l'intérieur du Bo."""
    __slots__ = ('picks_a', 'picks_b', 'bans_a', 'bans_b', 'winner')

    def __init__(self):
        self.picks_a: List[str] = []
        self.picks_b: List[str] = []
        self.bans_a:  List[str] = []
        self.bans_b:  List[str] = []
        self.winner:  Optional[str] = None  # "A" ou "B"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "picks_a": self.picks_a,
            "picks_b": self.picks_b,
            "bans_a":  self.bans_a,
            "bans_b":  self.bans_b,
            "winner":  self.winner,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Game":
        g = cls()
        g.picks_a = d.get("picks_a", [])
        g.picks_b = d.get("picks_b", [])
        g.bans_a  = d.get("bans_a", [])
        g.bans_b  = d.get("bans_b", [])
        g.winner  = d.get("winner")
        return g


class SubstitutionRecord:
    """Enregistrement d'un remplacement de joueur."""
    __slots__ = ('game_number', 'team', 'out_id', 'in_id', 'was_captain', 'new_captain_id')

    def __init__(self, game_number: int, team: str, out_id: int, in_id: int,
                 was_captain: bool, new_captain_id: Optional[int] = None):
        self.game_number    = game_number
        self.team           = team
        self.out_id         = out_id
        self.in_id          = in_id
        self.was_captain    = was_captain
        self.new_captain_id = new_captain_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "game_number":    self.game_number,
            "team":           self.team,
            "out_id":         self.out_id,
            "in_id":          self.in_id,
            "was_captain":    self.was_captain,
            "new_captain_id": self.new_captain_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SubstitutionRecord":
        return cls(
            game_number    = d["game_number"],
            team           = d["team"],
            out_id         = d["out_id"],
            in_id          = d["in_id"],
            was_captain    = d["was_captain"],
            new_captain_id = d.get("new_captain_id"),
        )


class SeriesState:
    """État global de la série — stocké uniquement en RAM."""
    __slots__ = (
        'id', 'bo', 'team_a', 'team_b', 'captain_a', 'captain_b',
        'blue_side', 'score_a', 'score_b', 'fearless_pool', 'games',
        'guild', 'captain_a_name', 'captain_b_name', 'status_msg_id',
        'substitutions', 'started_at', 'ended_at',
    )

    def __init__(self, id: str, bo: int, team_a: List[int], team_b: List[int],
                 captain_a: int, captain_b: int, blue_side: str = "A",
                 score_a: int = 0, score_b: int = 0):
        self.id         = id
        self.bo         = bo
        self.team_a     = team_a
        self.team_b     = team_b
        self.captain_a  = captain_a
        self.captain_b  = captain_b
        self.blue_side  = blue_side
        self.score_a    = score_a
        self.score_b    = score_b
        self.fearless_pool: Set[str]                 = set()
        self.games:         List[Game]               = [Game()]
        self.substitutions: List[SubstitutionRecord] = []

        self.guild:            Optional[object] = None
        self.captain_a_name:   str              = "Cap A"
        self.captain_b_name:   str              = "Cap B"
        self.status_msg_id:    Optional[int]    = None

        self.started_at: float           = time.time()
        self.ended_at:   Optional[float] = None

    # ── Constructeur ──────────────────────────────────────────────────────
    @classmethod
    def new(cls, bo: int, team_a: List[int], team_b: List[int],
            captain_a: int, captain_b: int) -> "SeriesState":
        return cls(
            id        = str(uuid.uuid4())[:8],
            bo        = bo,
            team_a    = team_a,
            team_b    = team_b,
            captain_a = captain_a,
            captain_b = captain_b,
        )

    # ── Propriété ─────────────────────────────────────────────────────────
    @property
    def current_game(self) -> Game:
        return self.games[-1]

    # ── Logique série ─────────────────────────────────────────────────────
    def start_new_game(self) -> None:
        self.games.append(Game())

    def finished(self) -> bool:
        target = self.bo // 2 + 1
        return self.score_a >= target or self.score_b >= target

    def winner_side(self) -> Optional[str]:
        if not self.finished():
            return None
        return "A" if self.score_a > self.score_b else "B"

    def get_all_picked_champs(self) -> Set[str]:
        return {c for g in self.games for c in g.picks_a + g.picks_b}

    def swap_sides(self) -> None:
        self.team_a,         self.team_b         = self.team_b,         self.team_a
        self.captain_a,      self.captain_b      = self.captain_b,      self.captain_a
        self.captain_a_name, self.captain_b_name = self.captain_b_name, self.captain_a_name
        self.score_a,        self.score_b        = self.score_b,        self.score_a

    # ── Remplacement joueur ────────────────────────────────────────────────
    def substitute(self, out_id: int, in_id: int) -> SubstitutionRecord:
        """
        Remplace out_id par in_id dans la bonne équipe.
        Si out_id est capitaine, tire un nouveau cap aléatoire dans l'équipe.
        Lève ValueError si out_id introuvable.
        """
        game_number = len(self.games)

        if out_id in self.team_a:
            team_tag, lst = "A", self.team_a
        elif out_id in self.team_b:
            team_tag, lst = "B", self.team_b
        else:
            raise ValueError(f"{out_id} n'est dans aucune équipe")

        lst[lst.index(out_id)] = in_id

        was_captain    = out_id in (self.captain_a, self.captain_b)
        new_captain_id: Optional[int] = None

        if was_captain:
            pool = [p for p in lst if p != in_id] or [in_id]
            new_cap = random.choice(pool)
            new_captain_id = new_cap
            if out_id == self.captain_a:
                self.captain_a = new_cap
            else:
                self.captain_b = new_cap

        rec = SubstitutionRecord(game_number, team_tag, out_id, in_id, was_captain, new_captain_id)
        self.substitutions.append(rec)
        return rec

    # ── Sérialisation historique Redis ────────────────────────────────────
    def to_history_dict(self) -> Dict[str, Any]:
        return {
            "id":            self.id,
            "bo":            self.bo,
            "team_a":        self.team_a,
            "team_b":        self.team_b,
            "captain_a":     self.captain_a,
            "captain_b":     self.captain_b,
            "score_a":       self.score_a,
            "score_b":       self.score_b,
            "winner":        self.winner_side(),
            "games":         [g.to_dict() for g in self.games],
            "fearless":      list(self.fearless_pool),
            "substitutions": [s.to_dict() for s in self.substitutions],
            "started_at":    self.started_at,
            "ended_at":      self.ended_at or time.time(),
        }

    @classmethod
    def from_history_dict(cls, d: Dict[str, Any]) -> "SeriesState":
        """Désérialise depuis Redis — usage lecture historique uniquement."""
        s = cls(
            id        = d["id"],
            bo        = d["bo"],
            team_a    = d["team_a"],
            team_b    = d["team_b"],
            captain_a = d["captain_a"],
            captain_b = d["captain_b"],
            score_a   = d["score_a"],
            score_b   = d["score_b"],
        )
        s.games         = [Game.from_dict(g) for g in d.get("games", [])]
        s.fearless_pool = set(d.get("fearless", []))
        s.substitutions = [SubstitutionRecord.from_dict(r) for r in d.get("substitutions", [])]
        s.started_at    = d.get("started_at", 0.0)
        s.ended_at      = d.get("ended_at")
        return s
