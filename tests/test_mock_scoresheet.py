import os
import tempfile
import unittest

from src.scoresheets import GoalChangeKind, GoalFeed, Player, ScoresheetError, Strength
from src.scoresheets.mock import is_mock_game, load_mock_game, loader


class MockGameTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, 'mock-game.yml')

    def write(self, text):
        with open(self.path, 'w') as f:
            f.write(text)

    def test_is_mock_game(self):
        self.assertTrue(is_mock_game('config/mock-game.yml'))
        self.assertTrue(is_mock_game('/app/config/mock-game.YAML'))
        self.assertFalse(is_mock_game('https://gamesheetstats.com/seasons/1/games/12345'))
        self.assertFalse(is_mock_game('https://example.com/game.yml'))

    def test_goal_details(self):
        self.write("""
goals:
  - period: 2
    clock: 12:28
    team: away
    scorer: 9 TESTY MCTESTFACE
    assists: [10 FAKEY FAKERSON, 11 SAMPLE VAN SKATERSON]
    strength: PP
    empty_net: true
""")
        goals, over = load_mock_game(self.path)

        self.assertFalse(over)
        self.assertEqual(len(goals), 1)
        g = goals[0]
        self.assertEqual((g.id, g.period, g.clock, g.team), ('mock-1', 2, '12:28', 'away'))
        self.assertEqual(g.scorer, Player('9', 'TESTY', 'MCTESTFACE'))
        self.assertEqual(g.assists, (Player('10', 'FAKEY', 'FAKERSON'), Player('11', 'SAMPLE', 'VAN SKATERSON')))
        self.assertEqual(g.strength, Strength.POWER_PLAY)
        self.assertTrue(g.empty_net)

    def test_defaults(self):
        self.write("goals:\n  - {team: home, scorer: 9, clock: '5:07'}\n")
        g, = load_mock_game(self.path)[0]

        self.assertEqual((g.period, g.clock, g.assists, g.strength, g.empty_net),
                         (1, '05:07', (), Strength.EVEN, False))
        self.assertEqual(g.scorer, Player('9', '', ''))

    def test_even_strength(self):
        for strength in ('even', 'EVEN', ''):
            with self.subTest(strength=strength):
                self.write(f"goals:\n  - {{team: home, scorer: 9, strength: '{strength}'}}\n")
                self.assertEqual(load_mock_game(self.path)[0][0].strength, Strength.EVEN)

    def test_empty_file_and_game_over(self):
        self.write('')
        self.assertEqual(load_mock_game(self.path), ([], False))
        self.write('over: true\n')
        self.assertEqual(load_mock_game(self.path), ([], True))

    def test_bad_files(self):
        for text in ('goals: [', 'goals: 3', '- 1', 'goals: [{team: left, scorer: 9}]',
                     'goals: [{team: home}]', "goals: [{team: home, scorer: 9, clock: '1:2:3'}]",
                     'goals: [{team: home, scorer: 9, strength: ppg}]'):
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(ScoresheetError):
                    load_mock_game(self.path)

    def test_missing_file(self):
        with self.assertRaises(ScoresheetError):
            load_mock_game(self.path)

    def test_feed_reports_edits(self):
        self.time = 1000.0
        feed = GoalFeed(loader(self.path), settle_seconds=10, now=lambda: self.time)
        self.write('goals: []\n')
        self.assertEqual(feed.poll(), [])

        self.write('goals: [{team: home, scorer: 9 TESTY MCTESTFACE, clock: 10:00}]\n')
        self.assertEqual([(c.kind, c.goal.id) for c in feed.poll()], [(GoalChangeKind.ADDED, 'mock-1')])

        self.time = 1005
        self.write('goals: [{team: home, scorer: 9 TESTY MCTESTFACE, assists: [10 FAKEY FAKERSON], clock: 10:00}]\n')
        self.assertEqual([(c.kind, c.goal.id) for c in feed.poll()], [(GoalChangeKind.UPDATED, 'mock-1')])

        self.time = 1015
        self.assertEqual([(c.kind, c.goal.id) for c in feed.poll()], [(GoalChangeKind.SETTLED, 'mock-1')])



if __name__ == '__main__':
    unittest.main()
