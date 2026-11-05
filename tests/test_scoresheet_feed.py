import unittest

from src.scoresheets import GoalChangeKind, GoalFeed, Player, ScoresheetError, Strength, open_feed
from src.scoresheets.feed import Goal, UNTIL_FURTHER_NOTICE, instant, strength_at

TESTY = Player('9', 'TESTY', 'MCTESTFACE')
FAKEY = Player('10', 'FAKEY', 'FAKERSON')
SAMPLE = Player('11', 'SAMPLE', 'SKATERSON')


def goal(goal_id, scorer=TESTY, assists=(), team='home'):
    return Goal(goal_id, 1, '10:00', team, scorer, tuple(assists), Strength.EVEN, False)


class GoalFeedTests(unittest.TestCase):
    def setUp(self):
        self.results = []
        self.time = 1000.0

        def load():
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        self.feed = GoalFeed(load, settle_seconds=10, now=lambda: self.time)

    def poll(self, *goals, at=None, over=False):
        if at is not None:
            self.time = at
        self.results.append((list(goals), over))
        return [(change.kind, change.goal.id) for change in self.feed.poll()]

    def test_reports_added_updated_and_removed_goals(self):
        first = goal('g1')
        second = goal('g2', FAKEY, team='away')

        self.assertEqual(self.poll(first), [(GoalChangeKind.ADDED, 'g1')])
        self.assertEqual(self.poll(first), [])
        self.assertEqual(self.poll(first, second), [(GoalChangeKind.ADDED, 'g2')])

        corrected = goal('g1', FAKEY, [TESTY])
        self.assertEqual(self.poll(corrected, second), [(GoalChangeKind.UPDATED, 'g1')])
        self.assertEqual(self.feed.goals[0].scorer.last_name, 'FAKERSON')

        self.assertEqual(self.poll(corrected), [(GoalChangeKind.REMOVED, 'g2')])
        self.assertEqual([g.id for g in self.feed.goals], ['g1'])

    def test_goal_settles_after_assists_stop_changing(self):
        self.poll(at=1000)
        scorer_only = goal('g1')
        one_assist = goal('g1', assists=[FAKEY])
        two_assists = goal('g1', assists=[FAKEY, SAMPLE])

        self.assertEqual(self.poll(scorer_only, at=1005), [(GoalChangeKind.ADDED, 'g1')])
        self.assertEqual(self.poll(one_assist, at=1010), [(GoalChangeKind.UPDATED, 'g1')])
        self.assertEqual(self.poll(two_assists, at=1015), [(GoalChangeKind.UPDATED, 'g1')])
        self.assertEqual(self.poll(two_assists, at=1020), [])
        self.assertFalse(self.feed.all_settled)
        self.assertFalse(self.feed.is_settled('g1'))

        # Settles 10 s after the last change.
        self.assertEqual(self.poll(two_assists, at=1025), [(GoalChangeKind.SETTLED, 'g1')])
        self.assertEqual(len(self.feed.goals[0].assists), 2)
        self.assertTrue(self.feed.all_settled)
        self.assertTrue(self.feed.is_settled('g1'))
        self.assertEqual(self.poll(two_assists, at=1100), [])

    def test_settled_goal_that_changes_settles_again(self):
        self.poll(at=1000)
        self.poll(goal('g1'), at=1001)
        self.assertEqual(self.poll(goal('g1'), at=1011), [(GoalChangeKind.SETTLED, 'g1')])

        corrected = goal('g1', FAKEY)
        self.assertEqual(self.poll(corrected, at=1100), [(GoalChangeKind.UPDATED, 'g1')])
        self.assertEqual(self.poll(corrected, at=1110), [(GoalChangeKind.SETTLED, 'g1')])

    def test_goals_already_scored_at_first_poll_count_as_settled(self):
        self.assertEqual(self.poll(goal('g1'), at=1000), [(GoalChangeKind.ADDED, 'g1')])
        self.assertTrue(self.feed.all_settled)
        self.assertEqual(self.poll(goal('g1'), at=1100), [])

    def test_removed_goal_does_not_settle(self):
        self.poll(at=1000)
        self.poll(goal('g1'), at=1001)

        self.assertEqual(self.poll(at=1020), [(GoalChangeKind.REMOVED, 'g1')])
        self.assertTrue(self.feed.all_settled)

    def test_game_over(self):
        self.poll()
        self.assertFalse(self.feed.game_over)
        self.poll(over=True)
        self.assertTrue(self.feed.game_over)

    def test_load_errors_leave_goals_unchanged(self):
        self.poll(goal('g1'))

        self.results.append(ScoresheetError('request failed: HTTP 503'))
        with self.assertRaises(ScoresheetError):
            self.feed.poll()
        self.assertEqual([g.id for g in self.feed.goals], ['g1'])


class StrengthAtTests(unittest.TestCase):
    # Detailed cases are in test_gamesheetstats, through GameSheet penalties.
    def test_strength_at(self):
        away_penalty = ('away', instant(1, 660), instant(1, 540))
        at = instant(1, 600)

        self.assertEqual(strength_at('home', at, []), Strength.EVEN)
        self.assertEqual(strength_at('home', at, [away_penalty]), Strength.POWER_PLAY)
        self.assertEqual(strength_at('away', at, [away_penalty]), Strength.SHORTHANDED)
        self.assertEqual(strength_at('home', instant(2, 1200), [away_penalty]), Strength.EVEN)

    def test_never_fewer_than_three_skaters(self):
        penalties = [('away', instant(1, 660), UNTIL_FURTHER_NOTICE)] * 3
        penalties.append(('home', instant(1, 660), UNTIL_FURTHER_NOTICE))

        self.assertEqual(strength_at('home', instant(1, 600), penalties), Strength.POWER_PLAY)


class OpenFeedTests(unittest.TestCase):
    def test_rejects_unsupported_urls(self):
        for url in ('https://example.com/game/1', 'https://pdf.play.spordle.com/game/1', ''):
            with self.subTest(url=url), self.assertRaises(ValueError):
                open_feed(url)

    def test_rejects_gamesheetstats_url_without_a_game(self):
        with self.assertRaises(ValueError):
            open_feed('https://gamesheetstats.com/seasons/1/standings')

    def test_opens_gamesheetstats_and_mock_games(self):
        self.assertIsInstance(open_feed('https://gamesheetstats.com/seasons/1/games/12345'), GoalFeed)
        self.assertIsInstance(open_feed('mock-game.yml', settle_seconds=3), GoalFeed)


if __name__ == '__main__':
    unittest.main()
