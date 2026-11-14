"""Goals entered by hand, for when the online scoresheet isn't being updated
(an arena with no WiFi, say).

A manual goal belongs to a team by name (the overlay team it was entered
for), not a scoresheet side. It has the same attributes as a ``Goal`` apart
from that, so captions treat both the same.

If the scoresheet catches up, ``ManualGoals.take_match`` finds the manual
goal a scoresheet goal replaces, so the goal isn't listed or shown twice.

A scoresheet goal can be corrected by hand too (``GoalOverrides``), for
when the scoresheet is wrong or missing details such as assists. The
correction is used until the scoresheet changes the goal.

Players are entered as a jersey number, a name, or both: "17", "17 Smith",
"Smith". Like scoresheets, manual goals can hold real names: don't store or
log them.
"""

import re
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

from .feed import Player, Strength

MAX_ASSISTS = 2
CLOCK_PATTERN = re.compile(r'^(\d{1,2}):([0-5]\d)$')
STRENGTHS = {
    '': Strength.EVEN,
    'even': Strength.EVEN,
    'pp': Strength.POWER_PLAY,
    'power_play': Strength.POWER_PLAY,
    'shg': Strength.SHORTHANDED,
    'shorthanded': Strength.SHORTHANDED,
}
# A scoresheet goal by a different scorer still replaces a manual goal this
# close to it in the same period: the scorer may have been entered wrongly.
MATCH_CLOCK_SECONDS = 30


@dataclass(frozen=True)
class ManualGoal:
    id: str
    team_name: str
    period: int
    clock: str  # Time remaining in the period, "MM:SS".
    scorer: Player
    assists: Tuple[Player, ...]
    strength: Strength
    empty_net: bool


def parse_player(text) -> Player:
    """"17 Smith" -> Player('17', 'Smith', ''). "#17" and "Smith" work too."""
    parts = str(text if text is not None else '').split()
    if parts and parts[0].lstrip('#').isdigit():
        jersey, names = parts[0].lstrip('#'), parts[1:]
    else:
        jersey, names = '', parts
    if not jersey and not names:
        raise ValueError('A player needs a jersey number or a name')
    return Player(jersey=jersey, first_name=' '.join(names[:1]), last_name=' '.join(names[1:]))


def player_text(player: Player) -> str:
    """The text ``parse_player`` reads back as the same player."""
    return ' '.join(part for part in (player.jersey, player.first_name, player.last_name) if part)


def parse_clock(text) -> str:
    match = CLOCK_PATTERN.match(str(text if text is not None else '').strip())
    if not match:
        raise ValueError(f'Time must be MM:SS: {text!r}')
    minutes, seconds = (int(part) for part in match.groups())
    return f'{minutes:02d}:{seconds:02d}'


def _clock_seconds(clock: str) -> int:
    minutes, seconds = clock.split(':')
    return int(minutes) * 60 + int(seconds)


def _parse_assists(data: dict) -> Tuple[Player, ...]:
    assists = data.get('assists') or []
    if isinstance(assists, str):
        assists = [assists]
    assists = [assist for assist in assists if str(assist or '').strip()]
    if len(assists) > MAX_ASSISTS:
        raise ValueError(f'A goal has at most {MAX_ASSISTS} assists')
    return tuple(parse_player(assist) for assist in assists)


def _parse(goal_id: str, team_name: str, data: dict) -> ManualGoal:
    try:
        period = int(data.get('period'))
    except (TypeError, ValueError) as e:
        raise ValueError('Period must be a number') from e
    if period < 1:
        raise ValueError('Period must be 1 or more')
    assists = _parse_assists(data)
    strength = STRENGTHS.get(str(data.get('strength') or '').strip().lower())
    if strength is None:
        raise ValueError('Strength must be even, pp or shg')
    if not str(data.get('scorer') or '').strip():
        raise ValueError('A goal needs a scorer')
    return ManualGoal(
        id=goal_id,
        team_name=team_name,
        period=period,
        clock=parse_clock(data.get('clock')),
        scorer=parse_player(data.get('scorer')),
        assists=assists,
        strength=strength,
        empty_net=bool(data.get('empty_net')),
    )


def _same_player(a: Player, b: Player) -> bool:
    if a.jersey and b.jersey:
        return a.jersey.lstrip('0') == b.jersey.lstrip('0')
    # Without both numbers, a name entered by hand matches the scoresheet's
    # surname or first name.
    names = {name.upper() for name in (b.first_name, b.last_name) if name}
    return any(name.upper() in names for name in (a.first_name, a.last_name) if name)


class ManualGoals:
    """The goals entered by hand, in game order."""

    def __init__(self) -> None:
        self._goals: Dict[str, ManualGoal] = {}
        self._next_id = 1

    @property
    def goals(self) -> List[ManualGoal]:
        return sorted(self._goals.values(), key=lambda goal: (goal.period, -_clock_seconds(goal.clock)))

    def get(self, goal_id: str) -> Optional[ManualGoal]:
        return self._goals.get(goal_id)

    def add(self, team_name: str, data: dict) -> ManualGoal:
        """Adds a goal from the entry form's fields: period, clock, scorer,
        assists, strength ("even", "pp", "shg") and empty_net.

        Raises ``ValueError`` for a field that can't be used.
        """
        goal = _parse(f'manual-{self._next_id}', team_name, data)
        self._next_id += 1
        self._goals[goal.id] = goal
        return goal

    def update(self, goal_id: str, data: dict) -> ManualGoal:
        """Replaces a goal's fields; it keeps its team. Raises ``KeyError``
        for an unknown goal and ``ValueError`` like ``add``."""
        goal = _parse(goal_id, self._goals[goal_id].team_name, data)
        self._goals[goal_id] = goal
        return goal

    def remove(self, goal_id: str) -> Optional[ManualGoal]:
        return self._goals.pop(goal_id, None)

    def take_match(self, team_name: str, goal) -> Optional[ManualGoal]:
        """Removes and returns the manual goal that a scoresheet goal by
        ``team_name`` replaces, if there is one.

        A match is in the same period, and has the same scorer or is within
        ``MATCH_CLOCK_SECONDS``. The nearest one in time wins, preferring a
        scorer match.
        """
        at = _clock_seconds(goal.clock)
        candidates = []
        for manual in self._goals.values():
            if manual.team_name != team_name or manual.period != goal.period:
                continue
            distance = abs(_clock_seconds(manual.clock) - at)
            same_scorer = _same_player(manual.scorer, goal.scorer)
            if same_scorer or distance <= MATCH_CLOCK_SECONDS:
                candidates.append((not same_scorer, distance, manual.id))
        if not candidates:
            return None
        return self._goals.pop(min(candidates)[2])


class GoalOverrides:
    """Corrections entered by hand for scoresheet goals, by goal ID.

    A correction replaces everything the entry form has (period, clock,
    scorer, assists, strength, empty net); the goal keeps its ID and team.
    Each is kept with the scoresheet's goal as it was when entered. Once the
    scoresheet changes the goal, it has caught up, and the correction is
    dropped.
    """

    def __init__(self) -> None:
        # Goal ID -> (the scoresheet's goal then, the corrected goal).
        self._overrides: Dict[str, Tuple[object, object]] = {}

    def has(self, goal_id: str) -> bool:
        return goal_id in self._overrides

    def set(self, goal, data: dict) -> None:
        """Corrects a scoresheet goal from the entry form's fields. Raises
        ``ValueError`` like ``ManualGoals.add``."""
        fields = _parse(goal.id, '', data)
        corrected = replace(
            goal,
            period=fields.period,
            clock=fields.clock,
            scorer=fields.scorer,
            assists=fields.assists,
            strength=fields.strength,
            empty_net=fields.empty_net,
        )
        self._overrides[goal.id] = (goal, corrected)

    def remove(self, goal_id: str) -> bool:
        """Goes back to the scoresheet's goal. Returns whether it was corrected."""
        return self._overrides.pop(goal_id, None) is not None

    def apply(self, goals) -> list:
        """The scoresheet's goals, with the corrections entered by hand.
        Drops the corrections of goals the scoresheet has changed."""
        result = []
        for goal in goals:
            override = self._overrides.get(goal.id)
            if override is not None and override[0] != goal:
                del self._overrides[goal.id]
                override = None
            result.append(override[1] if override else goal)
        return result
