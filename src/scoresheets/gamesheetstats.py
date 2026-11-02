"""Reads goals from a GameSheet game, for showing who scored on the overlay.

gamesheetstats.com game pages get their live data from a public Firestore
document per game (``games/{game_id}/schemas/GameSchema/versions/v2`` in the
``gamesheet-production`` project). This is undocumented, so everything read
from it is checked, and anything unexpected raises ``GameSheetError``.

Only goal and penalty events are used, and only the goal details are kept.
The document also holds rosters and officials' contact details: don't store
or log it.

Notes on the document, from captured game data:

* ``time.clock`` is ``PP:MM:SS``: period, then time remaining in it.
* ``time.real`` is local time with a misleading ``Z`` suffix, so it's only
  used to break ties.
* Deleted events are left behind as ``GameEvent.IdOnly`` entries.
* A penalty's ``penalty.off`` and ``penalty.on`` clocks are when the player
  left and is due back. GameSheet moves ``on`` to the goal's time when a
  power-play goal releases the player early. A penalty that runs into the
  next period keeps the old period number with a larger time remaining
  (``02:00:10`` off, ``02:18:10`` on), so the period has to be bumped.
  ``on`` is null for a few seconds while the scorekeeper enters a penalty.
* The scorekeeper enters a goal's scorer first and adds assists a few
  seconds later, so a new goal is usually followed by updates to it.
  ``GoalFeed`` reports a goal as settled once it stops changing.
* ``data.game.status`` is ``in progress`` during the game and
  ``unofficial`` once it ends, when ``data.game.endTime`` is also set.
* Goals don't say whether they were power play or shorthanded, so that's
  worked out by counting the penalties being served when the goal was
  scored.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

FIRESTORE_DOCUMENT_URL = (
    'https://firestore.googleapis.com/v1/projects/gamesheet-production/'
    'databases/(default)/documents/games/{game_id}/schemas/GameSchema/versions/v2'
)
GAME_URL_PATTERN = re.compile(r'/games/(\d+)(?:[/?#]|$)')
CLOCK_PATTERN = re.compile(r'^(\d+):(\d+):(\d+)$')

BASE_SKATERS = 5
MIN_SKATERS = 3
UNTIL_FURTHER_NOTICE = (float('inf'), 0)
FINISHED_GAME_STATUSES = {'unofficial', 'official', 'final'}
DEFAULT_SETTLE_SECONDS = 10


class GameSheetError(Exception):
    """The game couldn't be read, or its data wasn't in the expected shape."""


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


def game_id_from_url(url: str) -> str:
    """Returns the game ID from a gamesheetstats.com game URL."""
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname or not parts.hostname.endswith('gamesheetstats.com'):
        raise ValueError(f'Not a gamesheetstats.com URL: {url}')
    match = GAME_URL_PATTERN.search(parts.path)
    if not match:
        raise ValueError(f'No game ID in GameSheet URL: {url}')
    return match.group(1)


def document_url(game_id: str, api_key: Optional[str] = None) -> str:
    url = FIRESTORE_DOCUMENT_URL.format(game_id=urllib.parse.quote(game_id))
    if api_key:
        url += '?' + urllib.parse.urlencode({'key': api_key})
    return url


def fetch_document(game_id: str, api_key: Optional[str] = None, timeout: float = 10) -> dict:
    """Fetches a game's Firestore document, as Firestore's REST API returns it."""
    try:
        with urllib.request.urlopen(document_url(game_id, api_key), timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise GameSheetError(f'GameSheet request failed: HTTP {e.code}') from e
    except (urllib.error.URLError, OSError) as e:
        raise GameSheetError(f'GameSheet request failed: {e}') from e
    except ValueError as e:
        raise GameSheetError('GameSheet response was not JSON') from e


def decode_value(value: dict):
    """Converts a Firestore typed value (``{"stringValue": "x"}``) to plain Python."""
    if not isinstance(value, dict) or len(value) != 1:
        raise GameSheetError(f'Unexpected Firestore value: {value!r}')
    (kind, inner), = value.items()
    if kind == 'mapValue':
        return decode_fields(inner.get('fields', {}))
    if kind == 'arrayValue':
        return [decode_value(item) for item in inner.get('values', [])]
    if kind == 'integerValue':
        return int(inner)
    if kind == 'nullValue':
        return None
    return inner


def decode_fields(fields: dict) -> dict:
    return {name: decode_value(value) for name, value in fields.items()}


def _decode_document(document: dict) -> dict:
    if not isinstance(document, dict) or not isinstance(document.get('fields'), dict):
        raise GameSheetError('GameSheet document has no fields')
    return decode_fields(document['fields'])


def game_is_over(document: dict) -> bool:
    data = _decode_document(document).get('data')
    game = data.get('game') if isinstance(data, dict) else None
    if not isinstance(game, dict):
        raise GameSheetError('GameSheet document has no game details')
    status = str(game.get('status') or '').lower()
    return bool(game.get('endTime')) or status in FINISHED_GAME_STATUSES


def goals_from_document(document: dict) -> List[Goal]:
    """Returns the game's goals, in the order they were scored."""
    game = _decode_document(document)
    events = game.get('events')
    if not isinstance(events, dict):
        raise GameSheetError('GameSheet document has no events')

    penalties = []
    goal_events = []
    for event in events.values():
        kind = event.get('type') if isinstance(event, dict) else None
        if not isinstance(kind, str):
            raise GameSheetError('GameSheet event has no type')
        if kind == 'GameEvent.HockeyPenalty':
            penalty = _penalty_interval(event)
            if penalty is not None:
                penalties.append(penalty)
        elif _is_goal_type(kind):
            goal_events.append(event)

    goals = [(_event_order(event), _goal(event, penalties)) for event in goal_events]
    return [goal for _, goal in sorted(goals, key=lambda item: item[0])]


class GoalFeed:
    """Polls a GameSheet game and reports goals added, changed or removed.

    A goal is reported as settled once it has gone ``settle_seconds`` without
    changing, so the scorekeeper has had time to add the assists. It's
    checked on each ``poll``, so call ``poll`` more often than that. A settled
    goal that changes again is reported as updated, then settled again.

    The first ``poll`` reports every goal so far as added, and treats them as
    already settled. ``fetch`` and ``now`` are replaceable for tests.
    """

    def __init__(
        self,
        url: str,
        api_key: Optional[str] = None,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        fetch: Callable[[str, Optional[str]], dict] = fetch_document,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.game_id = game_id_from_url(url)
        self.api_key = api_key
        self.settle_seconds = settle_seconds
        self.game_over = False
        self._fetch = fetch
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

    def poll(self) -> List[GoalChange]:
        document = self._fetch(self.game_id, self.api_key)
        goals = goals_from_document(document)
        game_over = game_is_over(document)
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


def _is_goal_type(kind: str) -> bool:
    # Seen: GameEvent.TeamShooterGoalOnProtectedNet. Other goal kinds (empty
    # net, penalty shot) are assumed to follow the same naming. Shootout
    # attempts aren't goals for the overlay.
    return kind.startswith('GameEvent.') and 'Goal' in kind and 'Shootout' not in kind


def _team(value) -> str:
    vs = value.get('vs') if isinstance(value, dict) else None
    if vs == 'home':
        return 'home'
    if vs == 'visitor':
        return 'away'
    raise GameSheetError(f'Unexpected GameSheet team: {vs!r}')


def _player(value) -> Player:
    if not isinstance(value, dict):
        raise GameSheetError('GameSheet goal has no scorer')
    return Player(
        jersey=str(value.get('jersey') or ''),
        first_name=str(value.get('firstName') or ''),
        last_name=str(value.get('lastName') or ''),
    )


def _parse_clock(clock) -> Tuple[int, int]:
    """Returns (period, seconds remaining) for a ``PP:MM:SS`` clock."""
    match = CLOCK_PATTERN.match(clock) if isinstance(clock, str) else None
    if not match:
        raise GameSheetError(f'Unexpected GameSheet clock: {clock!r}')
    period, minutes, seconds = (int(part) for part in match.groups())
    return period, minutes * 60 + seconds


def _instant(period: int, remaining: int) -> Tuple[int, int]:
    # Sorts in game order without needing the period length.
    return period, -remaining


def _event_clock(event) -> Tuple[int, int]:
    time = event.get('time')
    return _parse_clock(time.get('clock') if isinstance(time, dict) else None)


def _event_order(event):
    period, remaining = _event_clock(event)
    return _instant(period, remaining), str(event['time'].get('real') or '')


def _penalty_interval(event) -> Optional[Tuple[str, Tuple[int, int], Tuple[int, int]]]:
    """Returns (team, off instant, on instant), or None if it doesn't change strength."""
    penalty = event.get('penalty')
    if not isinstance(penalty, dict):
        raise GameSheetError('GameSheet penalty has no details')
    if 'misconduct' in str(penalty.get('penalty_class') or '').lower():
        return None
    off, on = penalty.get('off'), penalty.get('on')
    if not isinstance(off, dict):
        raise GameSheetError('GameSheet penalty has no off time')
    off_period, off_remaining = _parse_clock(off.get('clock'))
    if on is None:
        # Saved while the scorekeeper is still entering it: "on" is null
        # until the length is filled in. Count it as still being served.
        on_instant = UNTIL_FURTHER_NOTICE
    elif isinstance(on, dict):
        on_period, on_remaining = _parse_clock(on.get('clock'))
        if on_period == off_period and on_remaining > off_remaining:
            # Ran into the next period; see the module docstring.
            on_period += 1
        on_instant = _instant(on_period, on_remaining)
    else:
        raise GameSheetError('GameSheet penalty has an unexpected on time')
    team = _team(event.get('for', {}).get('team'))
    return team, _instant(off_period, off_remaining), on_instant


def _skaters(team: str, at: Tuple[int, int], penalties) -> int:
    # A penalty taken at the goal's time came after the goal; one that ends at
    # the goal's time was ended by it.
    serving = sum(1 for side, off, on in penalties if side == team and off < at <= on)
    return max(MIN_SKATERS, BASE_SKATERS - serving)


def _goal(event, penalties) -> Goal:
    scoring = event.get('for')
    if not isinstance(scoring, dict):
        raise GameSheetError('GameSheet goal has no scoring team')
    team = _team(scoring.get('team'))
    opponent = 'away' if team == 'home' else 'home'
    period, remaining = _event_clock(event)
    at = _instant(period, remaining)
    skaters = _skaters(team, at, penalties)
    opponent_skaters = _skaters(opponent, at, penalties)
    if skaters > opponent_skaters:
        strength = Strength.POWER_PLAY
    elif skaters < opponent_skaters:
        strength = Strength.SHORTHANDED
    else:
        strength = Strength.EVEN
    assists = scoring.get('assist') or []
    if not isinstance(assists, list):
        raise GameSheetError('GameSheet goal assists are not a list')
    event_id = event.get('id')
    if not isinstance(event_id, str):
        raise GameSheetError('GameSheet goal has no id')
    return Goal(
        id=event_id,
        period=period,
        clock=f'{remaining // 60:02d}:{remaining % 60:02d}',
        team=team,
        scorer=_player(scoring.get('scorer')),
        assists=tuple(_player(assist) for assist in assists),
        strength=strength,
        empty_net='EmptyNet' in event['type'],
    )


if __name__ == '__main__':
    import argparse
    from datetime import datetime

    parser = argparse.ArgumentParser(
        description='Print the goals in a GameSheet game as they change, until the game is over.')
    parser.add_argument('url', help='gamesheetstats.com game URL')
    parser.add_argument('--api-key', help='Firestore API key, if the request needs one')
    parser.add_argument('--interval', type=float, default=5, metavar='SECONDS',
                        help='seconds between polls (default: %(default)s)')
    parser.add_argument('--settle', type=float, default=DEFAULT_SETTLE_SECONDS, metavar='SECONDS',
                        help='seconds without changes before a goal is settled (default: %(default)s)')
    parser.add_argument('--once', action='store_true', help='print the goals so far and exit')
    args = parser.parse_args()

    def log(text: str) -> None:
        print(f'{datetime.now():%H:%M:%S} {text}', flush=True)

    def describe(goal: Goal) -> str:
        assists = ', '.join(f'#{p.jersey} {p.first_name} {p.last_name}' for p in goal.assists) or 'unassisted'
        tags = [label for label, on in (('PP', goal.strength == Strength.POWER_PLAY),
                                        ('SHG', goal.strength == Strength.SHORTHANDED),
                                        ('EN', goal.empty_net)) if on]
        return (f'P{goal.period} {goal.clock} {goal.team}: #{goal.scorer.jersey} '
                f'{goal.scorer.first_name} {goal.scorer.last_name} ({assists}) {" ".join(tags)}').rstrip()

    feed = GoalFeed(args.url, api_key=args.api_key, settle_seconds=args.settle)
    try:
        while True:
            try:
                for change in feed.poll():
                    log(f'{change.kind.value:8} {describe(change.goal)}')
            except GameSheetError as e:
                if args.once:
                    raise
                log(f'error    {e}')
            if args.once:
                break
            if feed.game_over and feed.all_settled:
                log('game over')
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
