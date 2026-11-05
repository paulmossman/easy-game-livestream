"""A mock game in a YAML file, for testing without a live game.

The file is read again on every poll, so editing and saving it is like the
scorekeeper entering goals::

    over: false            # true once the game has ended
    goals:
      - period: 1
        clock: 12:28       # time remaining
        team: home         # or away
        scorer: 17 TESTY MCTESTFACE
        assists: [4 FAKEY FAKERSON, 9 SAMPLE SKATERSON]
        strength: pp       # optional: pp or shg
        empty_net: true    # optional

Players are a jersey number then the names, in capitals like GameSheet. A
goal's ID is its position in the list, so changing a goal in place is an
update, and adding one to the end is a new goal.
"""

import re
from typing import Callable, List, Tuple

from .feed import Goal, Player, ScoresheetError, Strength

CLOCK_PATTERN = re.compile(r'^(\d+):(\d\d)$')
STRENGTHS = {'pp': 'power_play', 'shg': 'shorthanded'}


def is_mock_game(url: str) -> bool:
    return url.lower().endswith(('.yml', '.yaml')) and '://' not in url


def _yaml():
    try:
        import yaml
    except ImportError as e:
        raise ScoresheetError('Mock games need PyYAML: pip install PyYAML') from e
    return yaml


def load_mock_game(path: str) -> Tuple[List[Goal], bool]:
    """Returns the goals in a mock game file, and whether the game is over."""
    yaml = _yaml()
    try:
        with open(path) as f:
            game = yaml.safe_load(f) or {}
    except OSError as e:
        raise ScoresheetError(f'Mock game file could not be read: {e}') from e
    except yaml.YAMLError as e:
        raise ScoresheetError(f'Mock game file is not valid YAML: {e}') from e
    if not isinstance(game, dict):
        raise ScoresheetError('Mock game file must be a mapping')
    goals = game.get('goals') or []
    if not isinstance(goals, list):
        raise ScoresheetError('Mock game goals must be a list')
    return [_goal(index, goal) for index, goal in enumerate(goals, 1)], bool(game.get('over'))


def loader(path: str) -> Callable[[], Tuple[List[Goal], bool]]:
    """Returns a ``GoalFeed`` load function for a mock game file."""
    _yaml()  # Fail now, not on every poll.
    return lambda: load_mock_game(path)


def _player(value) -> Player:
    parts = str(value if value is not None else '').split()
    if not parts:
        raise ScoresheetError('Mock goal has an empty player')
    jersey, names = (parts[0], parts[1:]) if parts[0].isdigit() else ('', parts)
    return Player(jersey=jersey, first_name=' '.join(names[:1]), last_name=' '.join(names[1:]))


def _clock(value) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        # YAML 1.1 reads an unquoted 12:28 as the number 748 (base 60).
        minutes, seconds = divmod(value, 60)
    else:
        match = CLOCK_PATTERN.match(str(value))
        if not match:
            raise ScoresheetError(f'Mock goal clock must be MM:SS: {value!r}')
        minutes, seconds = (int(part) for part in match.groups())
    return f'{minutes:02d}:{seconds:02d}'


def _goal(index: int, goal) -> Goal:
    if not isinstance(goal, dict):
        raise ScoresheetError(f'Mock goal {index} must be a mapping')
    team = goal.get('team')
    if team not in ('home', 'away'):
        raise ScoresheetError(f'Mock goal {index} team must be home or away')
    if 'scorer' not in goal:
        raise ScoresheetError(f'Mock goal {index} has no scorer')
    assists = goal.get('assists') or []
    if not isinstance(assists, list):
        assists = [assists]
    strength = str(goal.get('strength') or '').lower()
    if strength and strength not in STRENGTHS:
        raise ScoresheetError(f'Mock goal {index} strength must be pp or shg')
    try:
        period = int(goal.get('period', 1))
    except (TypeError, ValueError) as e:
        raise ScoresheetError(f'Mock goal {index} period must be a number') from e
    return Goal(
        id=f'mock-{index}',
        period=period,
        clock=_clock(goal.get('clock', '00:00')),
        team=team,
        scorer=_player(goal['scorer']),
        assists=tuple(_player(assist) for assist in assists),
        strength=Strength(STRENGTHS.get(strength, 'even')),
        empty_net=bool(goal.get('empty_net')),
    )
