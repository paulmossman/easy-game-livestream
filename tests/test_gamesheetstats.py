import unittest
import urllib.error
from unittest import mock

from src.scoresheets import GameNotStarted, GoalChangeKind, GoalFeed, Player, ScoresheetError, Strength
from src.scoresheets.gamesheetstats import (
    decode_value,
    document_url,
    fetch_document,
    game_id_from_url,
    game_is_over,
    goals_from_document,
    is_game_url,
    loader,
)

GAME_URL = 'https://gamesheetstats.com/seasons/1/games/12345'

HOME = {'vs': 'home', 'id': 1, 'details': {'title': 'Home Testers', 'logo': ''},
        'record': {'wins': 1, 'losses': 0}}
AWAY = {'vs': 'visitor', 'id': 2, 'details': {'title': 'Visiting Fakes', 'logo': ''},
        'record': {'wins': 0, 'losses': 1}}


def encode(value):
    """Encodes plain Python the way Firestore's REST API does."""
    if isinstance(value, dict):
        return {'mapValue': {'fields': {k: encode(v) for k, v in value.items()}}}
    if isinstance(value, list):
        return {'arrayValue': {'values': [encode(v) for v in value]}} if value else {'arrayValue': {}}
    if isinstance(value, bool):
        return {'booleanValue': value}
    if isinstance(value, int):
        return {'integerValue': str(value)}
    if value is None:
        return {'nullValue': None}
    return {'stringValue': value}


def player(jersey, first, last):
    return {'jersey': jersey, 'firstName': first, 'lastName': last, 'id': int(jersey or 0),
            'position': 'forward', 'status': 'playing', 'photoURL': '',
            'suspension': {'length': 0, 'number': 0}}


TESTY = player('9', 'TESTY', 'MCTESTFACE')
FAKEY = player('10', 'FAKEY', 'FAKERSON')
SAMPLE = player('11', 'SAMPLE', 'SKATERSON')
DUMMY = player('20', 'DUMMY', 'DEFENCEMAN')
NOTREAL = player('21', 'NOTREAL', 'PERSONNAME')
GOALIE = player('1', 'PRETEND', 'GOALIEGUY')


def time(clock, real='2026-01-01T12:00:00Z'):
    return {'clock': clock, 'real': real, 'running': False}


def goal(event_id, clock, team, scorer, assists=(), kind='TeamShooterGoalOnProtectedNet', real='2026-01-01T12:00:00Z'):
    against = AWAY if team is HOME else HOME
    return {
        'id': event_id,
        'type': f'GameEvent.{kind}',
        'time': time(clock, real),
        'for': {'team': team, 'scorer': scorer, 'assist': list(assists)},
        'against': {'team': against, 'goalie': GOALIE},
    }


def penalty(event_id, team, off, on, penalty_class='minor'):
    other = AWAY if team is HOME else HOME
    return {
        'id': event_id,
        'type': 'GameEvent.HockeyPenalty',
        'time': time(off),
        'for': {'team': team, 'player': DUMMY},
        # GameSheet's servedBy team doesn't always match; it must be ignored.
        'servedBy': {'team': other, 'player': DUMMY},
        'notes': [],
        'penalty': {'code': 'XX-MIN', 'label': 'Test penalty', 'length': '2',
                    'penalty_class': penalty_class, 'value': '',
                    'start': time(off), 'off': time(off), 'on': time(on)},
    }


def document(*events, status='in progress', end_time=None):
    game = {'status': status, 'startTime': '2026-01-01T12:00:00Z'}
    if end_time:
        game['endTime'] = end_time
    fields = {
        'sport': 'hockey',
        'schema': 'GameSchema',
        'version': 'v2',
        'events': {event['id']: event for event in events},
        'data': {'game': game, 'home': {'details': HOME['details']}, 'visitor': {'details': AWAY['details']}},
    }
    return {'name': 'projects/x/databases/(default)/documents/games/12345/schemas/GameSchema/versions/v2',
            'fields': {name: encode(value) for name, value in fields.items()}}


class UrlTests(unittest.TestCase):
    def test_game_id_from_url(self):
        self.assertEqual(game_id_from_url(GAME_URL), '12345')
        self.assertEqual(game_id_from_url(GAME_URL + '?tab=box-score'), '12345')
        self.assertEqual(game_id_from_url('https://www.gamesheetstats.com/seasons/1/games/12345/'), '12345')

    def test_game_id_from_url_rejects_other_urls(self):
        for url in ('https://example.com/seasons/1/games/12345',
                    'https://gamesheetstats.com/seasons/1/standings',
                    'https://gamesheetstats.com/seasons/1/games/abc'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                game_id_from_url(url)

    def test_document_url(self):
        self.assertTrue(document_url('12345').endswith('/documents/games/12345/schemas/GameSchema/versions/v2'))
        self.assertTrue(document_url('12345', 'k&y').endswith('versions/v2?key=k%26y'))


class DecodeTests(unittest.TestCase):
    def test_decode_value(self):
        encoded = encode({'s': 'x', 'n': 3, 'b': True, 'z': None, 'a': [1, 'y'], 'e': [], 'm': {'k': 'v'}})
        self.assertEqual(decode_value(encoded),
                         {'s': 'x', 'n': 3, 'b': True, 'z': None, 'a': [1, 'y'], 'e': [], 'm': {'k': 'v'}})

    def test_decode_value_rejects_untyped_value(self):
        with self.assertRaises(ScoresheetError):
            decode_value({'stringValue': 'x', 'integerValue': '1'})


class GoalsFromDocumentTests(unittest.TestCase):
    def test_goal_details(self):
        goals = goals_from_document(document(goal('g1', '01:01:48', HOME, TESTY, [FAKEY, SAMPLE])))

        self.assertEqual(len(goals), 1)
        g = goals[0]
        self.assertEqual((g.id, g.period, g.clock, g.team), ('g1', 1, '01:48', 'home'))
        self.assertEqual(g.scorer, Player('9', 'TESTY', 'MCTESTFACE'))
        self.assertEqual(g.assists, (Player('10', 'FAKEY', 'FAKERSON'), Player('11', 'SAMPLE', 'SKATERSON')))
        self.assertEqual(g.strength, Strength.EVEN)
        self.assertFalse(g.empty_net)

    def test_unassisted_goal_for_visitors(self):
        g, = goals_from_document(document(goal('g1', '02:10:00', AWAY, DUMMY)))

        self.assertEqual(g.team, 'away')
        self.assertEqual(g.assists, ())

    def test_ignores_other_events(self):
        events = [
            goal('g1', '01:05:00', HOME, TESTY),
            {'id': 'deleted', 'type': 'GameEvent.IdOnly'},
            {'id': 'shift', 'type': 'GameEvent.PlayerShift', 'time': time('01:20:00'), 'team': HOME,
             'player': GOALIE, 'shift': 'start'},
            {'id': 'sub', 'type': 'GameEvent.Substitution', 'time': time('01:03:00'), 'team': HOME,
             'playerOut': GOALIE, 'playerIn': player('', 'Empty', 'Net')},
            penalty('p1', AWAY, '01:15:00', '01:13:00'),
        ]

        self.assertEqual([g.id for g in goals_from_document(document(*events))], ['g1'])

    def test_goals_are_in_game_order(self):
        goals = goals_from_document(document(
            goal('third', '03:19:59', HOME, TESTY),
            goal('late-first', '01:00:23', AWAY, DUMMY),
            goal('early-first', '01:15:00', HOME, FAKEY),
            goal('second', '02:10:00', AWAY, NOTREAL),
        ))

        self.assertEqual([g.id for g in goals], ['early-first', 'late-first', 'second', 'third'])

    def test_same_clock_goals_ordered_by_real_time(self):
        goals = goals_from_document(document(
            goal('b', '01:10:00', HOME, TESTY, real='2026-01-01T12:10:05Z'),
            goal('a', '01:10:00', HOME, FAKEY, real='2026-01-01T12:10:01Z'),
        ))

        self.assertEqual([g.id for g in goals], ['a', 'b'])

    def test_empty_net_goal_type(self):
        g, = goals_from_document(document(goal('g1', '03:00:30', AWAY, NOTREAL, kind='TeamShooterGoalOnEmptyNet')))

        self.assertTrue(g.empty_net)

    def test_shootout_goals_are_ignored(self):
        events = [goal('so', '05:00:00', HOME, TESTY, kind='ShootoutGoal')]

        self.assertEqual(goals_from_document(document(*events)), [])


class StrengthTests(unittest.TestCase):
    def strength(self, scoring_team, clock, *penalties):
        g, = goals_from_document(document(goal('g', clock, scoring_team, TESTY), *penalties))
        return g.strength

    def test_power_play_goal_ends_penalty_at_goal_time(self):
        # GameSheet sets the penalty's "on" to the goal's time.
        self.assertEqual(self.strength(HOME, '01:01:48', penalty('p', AWAY, '01:03:32', '01:01:48')),
                         Strength.POWER_PLAY)

    def test_shorthanded_goal(self):
        self.assertEqual(self.strength(HOME, '01:10:00', penalty('p', HOME, '01:11:00', '01:09:00')),
                         Strength.SHORTHANDED)

    def test_expired_penalty_is_even_strength(self):
        self.assertEqual(self.strength(HOME, '01:05:00', penalty('p', AWAY, '01:11:30', '01:09:30')),
                         Strength.EVEN)

    def test_penalty_taken_at_goal_time_is_after_the_goal(self):
        self.assertEqual(self.strength(HOME, '01:10:00', penalty('p', AWAY, '01:10:00', '01:08:00')),
                         Strength.EVEN)

    def test_penalty_carried_into_next_period(self):
        # Taken with 0:10 left in the 2nd: GameSheet gives "on" as 02:18:10,
        # meaning 18:10 of the 3rd.
        carried = penalty('p', HOME, '02:00:10', '02:18:10')

        self.assertEqual(self.strength(AWAY, '03:19:00', carried), Strength.POWER_PLAY)
        self.assertEqual(self.strength(AWAY, '03:18:00', carried), Strength.EVEN)

    def test_one_penalty_each_is_even_strength(self):
        self.assertEqual(self.strength(HOME, '01:10:00',
                                       penalty('h', HOME, '01:11:00', '01:09:00'),
                                       penalty('a', AWAY, '01:11:00', '01:09:00')),
                         Strength.EVEN)

    def test_five_on_three(self):
        self.assertEqual(self.strength(AWAY, '01:10:00',
                                       penalty('h1', HOME, '01:11:00', '01:09:00'),
                                       penalty('h2', HOME, '01:10:30', '01:08:30')),
                         Strength.POWER_PLAY)

    def test_penalty_still_being_entered_is_being_served(self):
        # GameSheet saves "on" as null until the scorekeeper fills in the length.
        unfinished = penalty('p', AWAY, '01:11:00', '01:09:00')
        unfinished['penalty']['on'] = None
        unfinished['penalty']['length'] = ''

        self.assertEqual(self.strength(HOME, '01:10:00', unfinished), Strength.POWER_PLAY)
        self.assertEqual(self.strength(HOME, '01:12:00', unfinished), Strength.EVEN)

    def test_misconduct_does_not_change_strength(self):
        self.assertEqual(self.strength(HOME, '01:10:00', penalty('p', AWAY, '01:11:00', '01:01:00', 'misconduct')),
                         Strength.EVEN)


class MalformedDocumentTests(unittest.TestCase):
    def test_missing_events(self):
        for doc in ({}, {'fields': {}}, {'fields': {'events': encode('x')}}):
            with self.subTest(doc=doc), self.assertRaises(ScoresheetError):
                goals_from_document(doc)

    def test_goal_without_scorer(self):
        bad = goal('g', '01:10:00', HOME, TESTY)
        del bad['for']['scorer']

        with self.assertRaises(ScoresheetError):
            goals_from_document(document(bad))

    def test_goal_with_unexpected_clock(self):
        with self.assertRaises(ScoresheetError):
            goals_from_document(document(goal('g', '10:00', HOME, TESTY)))

    def test_goal_with_unknown_team(self):
        bad = goal('g', '01:10:00', HOME, TESTY)
        bad['for']['team'] = dict(HOME, vs='neutral')

        with self.assertRaises(ScoresheetError):
            goals_from_document(document(bad))


class GameIsOverTests(unittest.TestCase):
    def test_game_is_over(self):
        self.assertFalse(game_is_over(document(status='in progress')))
        self.assertFalse(game_is_over(document(status='scheduled')))
        self.assertTrue(game_is_over(document(status='unofficial')))
        self.assertTrue(game_is_over(document(status='Official')))
        self.assertTrue(game_is_over(document(status='in progress', end_time='2026-01-01T14:00:00Z')))

    def test_missing_game_details(self):
        doc = document()
        del doc['fields']['data']
        with self.assertRaises(ScoresheetError):
            game_is_over(doc)


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.documents = []
        self.requests = []

        def fetch(game_id, api_key):
            self.requests.append((game_id, api_key))
            return self.documents.pop(0)

        self.feed = GoalFeed(loader(GAME_URL, api_key='key', fetch=fetch))

    def poll(self, *events, **game):
        self.documents.append(document(*events, **game))
        return [(change.kind, change.goal.id) for change in self.feed.poll()]

    def test_fetches_the_game_and_reads_its_goals(self):
        self.assertEqual(self.poll(goal('g1', '01:10:00', HOME, TESTY)), [(GoalChangeKind.ADDED, 'g1')])
        self.assertEqual(self.requests, [('12345', 'key')])
        self.assertFalse(self.feed.game_over)

        self.poll(goal('g1', '01:10:00', HOME, TESTY), status='unofficial')
        self.assertTrue(self.feed.game_over)

    def test_deleted_goal_is_removed(self):
        self.poll(goal('g1', '01:10:00', HOME, TESTY))

        # A deleted event is left behind as GameEvent.IdOnly.
        self.assertEqual(self.poll({'id': 'g1', 'type': 'GameEvent.IdOnly'}), [(GoalChangeKind.REMOVED, 'g1')])

    def test_penalty_entered_after_the_goal_updates_its_strength(self):
        g = goal('g1', '01:10:00', HOME, TESTY)
        self.poll(g)

        self.assertEqual(self.poll(g, penalty('p', AWAY, '01:11:00', '01:10:00')), [(GoalChangeKind.UPDATED, 'g1')])
        self.assertEqual(self.feed.goals[0].strength, Strength.POWER_PLAY)

    def test_is_game_url(self):
        self.assertTrue(is_game_url(GAME_URL))
        self.assertTrue(is_game_url('https://www.gamesheetstats.com/seasons/1/games/12345'))
        self.assertFalse(is_game_url('https://notgamesheetstats.com/seasons/1/games/12345'))
        self.assertFalse(is_game_url('mock-game.yml'))


class FetchDocumentTests(unittest.TestCase):
    def fail_with(self, code):
        error = urllib.error.HTTPError(document_url('12345'), code, 'error', {}, None)
        return mock.patch('urllib.request.urlopen', side_effect=error)

    def test_missing_document_means_not_started(self):
        with self.fail_with(404), self.assertRaises(GameNotStarted) as raised:
            fetch_document('12345')
        self.assertEqual(str(raised.exception), 'Game not started yet.')

    def test_other_http_errors(self):
        with self.fail_with(500), self.assertRaises(ScoresheetError) as raised:
            fetch_document('12345')
        self.assertNotIsInstance(raised.exception, GameNotStarted)
        self.assertEqual(str(raised.exception), 'GameSheet request failed: HTTP 500')


if __name__ == '__main__':
    unittest.main()
