"""Turns scoresheet goals into overlay captions, using the teams in the config.

A roster maps a team's jersey numbers to the text shown for those players,
such as "MacDonald". For a goal by a team with a roster, each player's
number is looked up there. Everyone else gets their scoresheet name with
simple capitalisation (scoresheets send names in capitals), so "MACDONALD"
becomes "Macdonald".

A team's ``scoresheet`` setting ("home" or "away") says which scoresheet side
it is. Otherwise the rosters show it: a side is the team whose roster its
goal scorers and assists match. Once one side is known, the other is
whichever overlay team name isn't that team.

The config's teams also give the overlay's starting team names, by their
``overlay_position``.

Goals are only read through their attributes (``scoresheets.Goal``), so this
module doesn't import ``scoresheets``.
"""

import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

STRENGTH_TAGS = {'power_play': 'PP', 'shorthanded': 'SHG'}
MAX_ASSISTS = 2
SCORESHEET_SIDES = {'home': 'home', 'away': 'away', 'visitor': 'away'}
OVERLAY_POSITIONS = ('left', 'right')
NAME_WORD_PATTERN = re.compile(r'[^\W\d_]+')


def capitalize_name(name: str) -> str:
    """"O'BRIEN-SMITH" -> "O'Brien-Smith"."""
    return NAME_WORD_PATTERN.sub(lambda match: match.group(0)[0].upper() + match.group(0)[1:].lower(), name)


def _name_key(name: str) -> str:
    return ''.join(character for character in name.upper() if character.isalpha())


def _jersey_key(jersey) -> str:
    return str(jersey).strip().lstrip('0') or '0'


def _roster_items(roster):
    # A mapping ({17: MacTestface}), or a list of them (- 17: MacTestface).
    if isinstance(roster, list):
        return [item for entry in roster if isinstance(entry, dict) for item in entry.items()]
    return list((roster or {}).items())


class Teams:
    """The ``teams`` config: a list of the game's two teams, each with a
    ``name``, ``overlay_position`` ("left" or "right"), roster ({jersey:
    display text}), Celebrate, and scoresheet side.

    ``overlay_position`` and ``scoresheet`` only need to be set for one
    team: the other team gets the other one. Without ``overlay_position``,
    the list order decides (the first team is on the left). Problems with the
    config are collected in ``warnings`` and that part is ignored.
    """

    def __init__(self, teams_config) -> None:
        self._rosters: Dict[str, Dict[str, str]] = {}
        self._celebrate: Dict[str, bool] = {}
        # Scoresheet side ("home", "away") -> team name, from the config.
        # The other side is worked out in side_teams, from the overlay team
        # names, so it follows a team renamed in the UI.
        self._sides: Dict[str, str] = {}
        # Overlay position ("left", "right") -> team name.
        self._positions: Dict[str, str] = {}
        self.warnings: List[str] = []
        unplaced = []
        for settings in self._team_entries(teams_config):
            name = settings['name']
            side_text = str(settings.get('scoresheet') or '').strip().lower()
            side = SCORESHEET_SIDES.get(side_text)
            if side_text and not side:
                self.warnings.append(f'Team {name!r}: scoresheet must be home or away, not {side_text!r}')
            elif side in self._sides:
                self.warnings.append(f'Team {name!r}: scoresheet {side} is already {self._sides[side]!r}')
            elif side:
                self._sides[side] = name
            position = str(settings.get('overlay_position') or '').strip().lower()
            if position and position not in OVERLAY_POSITIONS:
                self.warnings.append(f'Team {name!r}: overlay_position must be left or right, not {position!r}')
                position = ''
            elif position in self._positions:
                self.warnings.append(f'Team {name!r}: overlay_position {position} is already {self._positions[position]!r}')
                position = ''
            if position:
                self._positions[position] = name
            else:
                unplaced.append(name)
            self._rosters[name] = {
                _jersey_key(jersey): str(surname) for jersey, surname in _roster_items(settings.get('roster'))
                if surname
            }
            if 'celebrate' in settings:
                self._celebrate[name] = bool(settings['celebrate'])
        for name in unplaced:
            position = next(position for position in OVERLAY_POSITIONS if position not in self._positions)
            self._positions[position] = name

    def _team_entries(self, teams_config):
        # The list's entries that have a name, at most two, without repeats.
        if not teams_config:
            return []
        if not isinstance(teams_config, list):
            self.warnings.append('teams must be a list, with a name for each team; ignoring it')
            return []
        entries = []
        for settings in teams_config:
            name = str(settings.get('name') or '').strip() if isinstance(settings, dict) else ''
            if not name:
                self.warnings.append('Ignoring a team in teams without a name')
            elif any(entry['name'] == name for entry in entries):
                self.warnings.append(f'Ignoring a second team named {name!r}')
            elif len(entries) == len(OVERLAY_POSITIONS):
                self.warnings.append(f'Ignoring team {name!r}: teams has room for two')
            else:
                entries.append(dict(settings, name=name))
        return entries

    def overlay_name(self, position: str) -> Optional[str]:
        """The team the config puts at an overlay position ("left" or
        "right"), or None."""
        return self._positions.get(position)

    def celebrate(self, team_name: str) -> bool:
        """Whether the team's goals get a caption, unless changed in the UI."""
        if team_name in self._celebrate:
            return self._celebrate[team_name]
        return bool(self._rosters.get(team_name))

    def roster(self, team_name: str) -> Dict[str, str]:
        """The team's roster: {jersey number: display text}."""
        return dict(self._rosters.get(team_name, {}))

    def roster_match(self, player) -> Optional[Tuple[str, str]]:
        """Returns (team name, roster text) for a player on a roster.

        Matched on jersey number and surname (the roster text includes the
        scoresheet's surname), so another team's player with the same number
        doesn't match. Used to tell which team is which.
        """
        surname = _name_key(player.last_name)
        if not surname:
            return None
        for team_name, roster in self._rosters.items():
            text = roster.get(_jersey_key(player.jersey))
            if text and surname in _name_key(text):
                return team_name, text
        return None

    def player_name(self, player, team_name: Optional[str] = None) -> str:
        """The roster's text for the player's number, such as "MacTestface".

        Looks the number up in the scoring team's roster (``team_name``),
        else falls back to a roster match on number and surname. A player on
        no roster gets their scoresheet name, capitalised if it's in capitals:
        "Testy Mactestface". A player with only a number (entered by hand)
        is "#17".
        """
        text = self._rosters.get(team_name or '', {}).get(_jersey_key(player.jersey)) if player.jersey else None
        if not text:
            match = self.roster_match(player)
            text = match[1] if match else ' '.join(
                capitalize_name(part) if part.isupper() else part
                for part in (player.first_name, player.last_name) if part)
        if not text and player.jersey:
            text = f'#{player.jersey}'
        return text

    def side_teams(self, goals, left_team: str, right_team: str) -> Dict[str, Optional[str]]:
        """Returns the team name for each scoresheet side ("home", "away"), or None if unknown.

        A side set in the config wins; the rosters fill in the rest.
        """
        votes = {'home': Counter(), 'away': Counter()}
        placed = set(self._sides.values())
        for goal in goals:
            for player in (goal.scorer,) + tuple(goal.assists):
                match = self.roster_match(player)
                # A team the config puts on one side can't be the other.
                if match and match[0] not in placed:
                    votes[goal.team][match[0]] += 1
        found = {
            side: self._sides.get(side) or (counts.most_common(1)[0][0] if counts else None)
            for side, counts in votes.items()
        }
        sides = dict(found)
        for side, other in (('home', 'away'), ('away', 'home')):
            if found[side] is None and found[other] is not None:
                # Both overlay names are the same team until the operator
                # renames one, so there's no opponent name yet.
                opponents = {left_team, right_team} - {found[other]}
                if len(opponents) == 1:
                    sides[side] = opponents.pop()
        return sides

    def describe(self, goal, team_name: Optional[str] = None) -> dict:
        """The goal's scorer, assists and tags (PP, SHG, EN), as display text.

        ``team_name`` is the scoring team, whose roster names its players.
        """
        strength = getattr(goal.strength, 'value', goal.strength)
        tags = [STRENGTH_TAGS[strength]] if strength in STRENGTH_TAGS else []
        if goal.empty_net:
            tags.append('EN')
        return {
            'scorer': self.player_name(goal.scorer, team_name),
            'assists': [self.player_name(assist, team_name) for assist in goal.assists],
            # There were assists that weren't caught, so it isn't unassisted.
            'assists_unknown': bool(getattr(goal, 'assists_unknown', False)),
            'tags': tags,
        }

    def goal_text(self, goal, team_name: str, settled: bool = True) -> str:
        """Who scored a goal by ``team_name``: "MacTestface (Fakerson, ...) PP".

        Until the goal has settled, more assists may be on the way, so they
        end in "...". A goal whose assists weren't all caught (entered by
        hand) is never "unassisted": the assists that are known end in
        "...", and with none known there's no assist text at all.
        """
        description = self.describe(goal, team_name)
        assists = description['assists']
        unknown = description['assists_unknown']
        if (unknown or not settled) and len(assists) < MAX_ASSISTS:
            assists = assists + ['...']
        if unknown and not description['assists']:
            scorer_text = description['scorer']
        else:
            scorer_text = f'{description["scorer"]} ({", ".join(assists) or "unassisted"})'
        return ' '.join([scorer_text] + description['tags'])

    def caption(self, goal, team_name: str, settled: bool = True) -> str:
        """The one-line overlay caption for a goal by ``team_name``:
        "Testers Goal: MacTestface (Fakerson, ...) PP"."""
        return f'{team_name} Goal: {self.goal_text(goal, team_name, settled)}'
