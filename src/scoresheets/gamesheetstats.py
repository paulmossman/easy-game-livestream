"""Goals from gamesheetstats.com, GameSheet Inc.'s public game pages.

gamesheetstats.com game pages get their live data from a public Firestore
document per game (``games/{game_id}/schemas/GameSchema/versions/v2`` in the
``gamesheet-production`` project). This is undocumented, so everything read
from it is checked, and anything unexpected raises ``ScoresheetError``.

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
* The document doesn't exist (HTTP 404) until the scorekeeper starts the
  game, even though the game page is already up.
* Goals don't say whether they were power play or shorthanded, so that's
  worked out by counting the penalties being served when the goal was
  scored.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, Optional, Tuple

from .feed import GameNotStarted, Goal, Instant, Player, ScoresheetError, UNTIL_FURTHER_NOTICE, instant, strength_at

FIRESTORE_DOCUMENT_URL = (
    'https://firestore.googleapis.com/v1/projects/gamesheet-production/'
    'databases/(default)/documents/games/{game_id}/schemas/GameSchema/versions/v2'
)
GAME_URL_PATTERN = re.compile(r'/games/(\d+)(?:[/?#]|$)')
CLOCK_PATTERN = re.compile(r'^(\d+):(\d+):(\d+)$')
FINISHED_GAME_STATUSES = {'unofficial', 'official', 'final'}


def is_game_url(url: str) -> bool:
    hostname = urllib.parse.urlsplit(url).hostname or ''
    return hostname == 'gamesheetstats.com' or hostname.endswith('.gamesheetstats.com')


def loader(url: str, api_key: Optional[str] = None,
           fetch: Callable[[str, Optional[str]], dict] = None) -> Callable[[], Tuple[List[Goal], bool]]:
    """Returns a ``GoalFeed`` load function for a gamesheetstats.com game URL.

    ``fetch`` is replaceable for tests.
    """
    game_id = game_id_from_url(url)
    fetch = fetch or fetch_document

    def load():
        document = fetch(game_id, api_key)
        return goals_from_document(document), game_is_over(document)

    return load


def game_id_from_url(url: str) -> str:
    """Returns the game ID from a gamesheetstats.com game URL."""
    if not is_game_url(url):
        raise ValueError(f'Not a gamesheetstats.com URL: {url}')
    match = GAME_URL_PATTERN.search(urllib.parse.urlsplit(url).path)
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
        if e.code == 404:
            raise GameNotStarted() from e
        raise ScoresheetError(f'GameSheet request failed: HTTP {e.code}') from e
    except (urllib.error.URLError, OSError) as e:
        raise ScoresheetError(f'GameSheet request failed: {e}') from e
    except ValueError as e:
        raise ScoresheetError('GameSheet response was not JSON') from e


def decode_value(value: dict):
    """Converts a Firestore typed value (``{"stringValue": "x"}``) to plain Python."""
    if not isinstance(value, dict) or len(value) != 1:
        raise ScoresheetError(f'Unexpected Firestore value: {value!r}')
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
        raise ScoresheetError('GameSheet document has no fields')
    return decode_fields(document['fields'])


def game_is_over(document: dict) -> bool:
    data = _decode_document(document).get('data')
    game = data.get('game') if isinstance(data, dict) else None
    if not isinstance(game, dict):
        raise ScoresheetError('GameSheet document has no game details')
    status = str(game.get('status') or '').lower()
    return bool(game.get('endTime')) or status in FINISHED_GAME_STATUSES


def goals_from_document(document: dict) -> List[Goal]:
    """Returns the game's goals, in the order they were scored."""
    game = _decode_document(document)
    events = game.get('events')
    if not isinstance(events, dict):
        raise ScoresheetError('GameSheet document has no events')

    penalties = []
    goal_events = []
    for event in events.values():
        kind = event.get('type') if isinstance(event, dict) else None
        if not isinstance(kind, str):
            raise ScoresheetError('GameSheet event has no type')
        if kind == 'GameEvent.HockeyPenalty':
            penalty = _penalty_interval(event)
            if penalty is not None:
                penalties.append(penalty)
        elif _is_goal_type(kind):
            goal_events.append(event)

    goals = [(_event_order(event), _goal(event, penalties)) for event in goal_events]
    return [goal for _, goal in sorted(goals, key=lambda item: item[0])]


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
    raise ScoresheetError(f'Unexpected GameSheet team: {vs!r}')


def _player(value) -> Player:
    if not isinstance(value, dict):
        raise ScoresheetError('GameSheet goal has no scorer')
    return Player(
        jersey=str(value.get('jersey') or ''),
        first_name=str(value.get('firstName') or ''),
        last_name=str(value.get('lastName') or ''),
    )


def _parse_clock(clock) -> Tuple[int, int]:
    """Returns (period, seconds remaining) for a ``PP:MM:SS`` clock."""
    match = CLOCK_PATTERN.match(clock) if isinstance(clock, str) else None
    if not match:
        raise ScoresheetError(f'Unexpected GameSheet clock: {clock!r}')
    period, minutes, seconds = (int(part) for part in match.groups())
    return period, minutes * 60 + seconds


def _event_clock(event) -> Tuple[int, int]:
    time = event.get('time')
    return _parse_clock(time.get('clock') if isinstance(time, dict) else None)


def _event_order(event):
    period, remaining = _event_clock(event)
    return instant(period, remaining), str(event['time'].get('real') or '')


def _penalty_interval(event) -> Optional[Tuple[str, Instant, Instant]]:
    """Returns (team, off instant, on instant), or None if it doesn't change strength."""
    penalty = event.get('penalty')
    if not isinstance(penalty, dict):
        raise ScoresheetError('GameSheet penalty has no details')
    if 'misconduct' in str(penalty.get('penalty_class') or '').lower():
        return None
    off, on = penalty.get('off'), penalty.get('on')
    if not isinstance(off, dict):
        raise ScoresheetError('GameSheet penalty has no off time')
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
        on_instant = instant(on_period, on_remaining)
    else:
        raise ScoresheetError('GameSheet penalty has an unexpected on time')
    team = _team(event.get('for', {}).get('team'))
    return team, instant(off_period, off_remaining), on_instant


def _goal(event, penalties) -> Goal:
    scoring = event.get('for')
    if not isinstance(scoring, dict):
        raise ScoresheetError('GameSheet goal has no scoring team')
    team = _team(scoring.get('team'))
    period, remaining = _event_clock(event)
    assists = scoring.get('assist') or []
    if not isinstance(assists, list):
        raise ScoresheetError('GameSheet goal assists are not a list')
    event_id = event.get('id')
    if not isinstance(event_id, str):
        raise ScoresheetError('GameSheet goal has no id')
    return Goal(
        id=event_id,
        period=period,
        clock=f'{remaining // 60:02d}:{remaining % 60:02d}',
        team=team,
        scorer=_player(scoring.get('scorer')),
        assists=tuple(_player(assist) for assist in assists),
        strength=strength_at(team, instant(period, remaining), penalties),
        empty_net='EmptyNet' in event['type'],
    )
