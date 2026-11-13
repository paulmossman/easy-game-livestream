"""Goals from a game's online scoresheet, whichever site publishes it.

Each source (``gamesheetstats``, ``mock``) provides a function that returns
the game's goals so far and whether the game is over. ``GoalFeed`` polls it
and reports what changed.
"""

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Iterable, List, Tuple

DEFAULT_SETTLE_SECONDS = 10
BASE_SKATERS = 5
MIN_SKATERS = 3


class ScoresheetError(Exception):
    """The game couldn't be read, or its data wasn't in the expected shape."""


class GameNotStarted(ScoresheetError):
    """The game has no scoresheet yet. Keep polling: it appears once it starts."""

    def __init__(self, message: str = 'Game not started yet.') -> None:
        super().__init__(message)


class Strength(str, Enum):
    EVEN = "even"
    POWER_PLAY = "power_play"
    SHORTHANDED = "shorthanded"


@dataclass(frozen=True)
class Player:
    jersey: str
    first_name: str
    last_name: str


@dataclass(frozen=True)
class Goal:
    id: str
    period: int
    clock: str  # Time remaining in the period, "MM:SS".
    team: str  # "home" or "away", matching penalties.Team values.
    scorer: Player
    assists: Tuple[Player, ...]
    strength: Strength
    empty_net: bool


class GoalChangeKind(str, Enum):
    ADDED = "added"
    UPDATED = "updated"
    SETTLED = "settled"
    REMOVED = "removed"


@dataclass(frozen=True)
class GoalChange:
    kind: GoalChangeKind
    goal: Goal


# A point in the game that sorts in game order without needing the period
# length: (period, -seconds remaining).
Instant = Tuple[float, int]
UNTIL_FURTHER_NOTICE: Instant = (float('inf'), 0)


def instant(period: int, remaining: int) -> Instant:
    return period, -remaining


def strength_at(team: str, at: Instant, penalties: Iterable[Tuple[str, Instant, Instant]]) -> Strength:
    """The scoring team's strength from (team, off, on) penalty intervals.

    For sources that give penalty times but don't mark goals as power play
    or shorthanded. Only penalties that change the number of skaters belong
    in ``penalties``.
    """
    penalties = list(penalties)
    opponent = 'away' if team == 'home' else 'home'
    skaters = _skaters(team, at, penalties)
    opponent_skaters = _skaters(opponent, at, penalties)
    if skaters > opponent_skaters:
        return Strength.POWER_PLAY
    if skaters < opponent_skaters:
        return Strength.SHORTHANDED
    return Strength.EVEN


def _skaters(team: str, at: Instant, penalties) -> int:
    # A penalty taken at the goal's time came after the goal; one that ends at
    # the goal's time was ended by it.
    serving = sum(1 for side, off, on in penalties if side == team and off < at <= on)
    return max(MIN_SKATERS, BASE_SKATERS - serving)


class GoalFeed:
    """Polls a game and reports goals added, changed or removed.

    ``load`` returns (goals in game order, whether the game is over), and
    raises ``ScoresheetError`` if the game can't be read.

    A goal is reported as settled once it has gone ``settle_seconds`` without
    changing, so the scorekeeper has had time to add the assists. It's
    checked on each ``poll``, so call ``poll`` more often than that. A settled
    goal that changes again is reported as updated, then settled again.

    The first ``poll`` reports every goal so far as added, and treats them as
    already settled. ``now`` is replaceable for tests.
    """

    def __init__(
        self,
        load: Callable[[], Tuple[List[Goal], bool]],
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settle_seconds = settle_seconds
        self.game_over = False
        self._load = load
        self._now = now
        self._polled = False
        self._goals: Dict[str, Goal] = {}
        self._changed_at: Dict[str, float] = {}
        self._settled = set()

    @property
    def goals(self) -> List[Goal]:
        return list(self._goals.values())

    @property
    def all_settled(self) -> bool:
        return all(goal_id in self._settled for goal_id in self._goals)

    def is_settled(self, goal_id: str) -> bool:
        return goal_id in self._settled

    def poll(self) -> List[GoalChange]:
        goals, game_over = self._load()
        now = self._now()
        current = {goal.id: goal for goal in goals}
        changes = [
            GoalChange(GoalChangeKind.REMOVED, goal)
            for goal_id, goal in self._goals.items()
            if goal_id not in current
        ]
        for goal in goals:
            previous = self._goals.get(goal.id)
            if previous is None:
                changes.append(GoalChange(GoalChangeKind.ADDED, goal))
            elif previous != goal:
                changes.append(GoalChange(GoalChangeKind.UPDATED, goal))
            else:
                continue
            self._changed_at[goal.id] = now
            if self._polled:
                self._settled.discard(goal.id)
            else:
                self._settled.add(goal.id)
        for goal in goals:
            if goal.id not in self._settled and now - self._changed_at[goal.id] >= self.settle_seconds:
                self._settled.add(goal.id)
                changes.append(GoalChange(GoalChangeKind.SETTLED, goal))
        self._goals = current
        self._changed_at = {goal_id: self._changed_at[goal_id] for goal_id in current}
        self._settled &= set(current)
        self._polled = True
        self.game_over = game_over
        return changes
