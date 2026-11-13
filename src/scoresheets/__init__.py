"""Goals from a game's online scoresheet, for showing who scored on the overlay.

``open_feed`` picks the source for a URL: a gamesheetstats.com game, or a
mock game file (``mock.py``). Another site needs a module with a ``loader``
like theirs, and a line in ``open_feed``.

Goals can also be entered by hand (``manual.py``) when the scoresheet isn't
being updated.

Scoresheets hold real players' names: don't store or log them.
"""

from .feed import (
    DEFAULT_SETTLE_SECONDS,
    Goal,
    GoalChange,
    GoalChangeKind,
    GoalFeed,
    GameNotStarted,
    Player,
    ScoresheetError,
    Strength,
)
from . import gamesheetstats, mock
from .manual import ManualGoal, ManualGoals, player_text
from .mock import is_mock_game


def open_feed(url: str, settle_seconds: float = DEFAULT_SETTLE_SECONDS, api_key=None) -> GoalFeed:
    """Returns a ``GoalFeed`` for a game URL or mock game file.

    Raises ``ValueError`` for a URL that no source handles, and
    ``ScoresheetError`` if the source can't be used.
    """
    if is_mock_game(url):
        load = mock.loader(url)
    elif gamesheetstats.is_game_url(url):
        load = gamesheetstats.loader(url, api_key)
    else:
        raise ValueError(f'Not a supported scoresheet URL: {url}')
    return GoalFeed(load, settle_seconds=settle_seconds)
