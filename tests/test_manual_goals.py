import unittest

from src.goal_captions import Teams
from src.scoresheets import Goal, Player, Strength
from src.scoresheets.manual import ManualGoals, parse_clock, parse_player, player_text

TEAMS = Teams({'Home Testers': {'roster': [{17: 'MacTestface'}, {4: 'Fakerson'}]}})


def entry(**fields):
    data = {'period': 2, 'clock': '12:28', 'scorer': '17', 'assists': ['4'], 'strength': 'even'}
    data.update(fields)
    return data


def scoresheet_goal(scorer, period=2, clock='12:20'):
    return Goal('sheet-1', period, clock, 'home', scorer, (), Strength.EVEN, False)


class ParseTests(unittest.TestCase):
    def test_parse_player(self):
        self.assertEqual(parse_player('17'), Player('17', '', ''))
        self.assertEqual(parse_player('#17 Testy MacTestface'), Player('17', 'Testy', 'MacTestface'))
        self.assertEqual(parse_player(' Fakerson '), Player('', 'Fakerson', ''))
        with self.assertRaises(ValueError):
            parse_player('  ')

    def test_player_text_round_trips(self):
        for text in ('17', '17 Testy MacTestface', 'Fakerson'):
            self.assertEqual(player_text(parse_player(text)), text)

    def test_parse_clock(self):
        self.assertEqual(parse_clock('8:05'), '08:05')
        self.assertEqual(parse_clock(' 12:28 '), '12:28')
        for bad in ('12:60', '1228', '', None):
            with self.assertRaises(ValueError):
                parse_clock(bad)


class ManualGoalsTests(unittest.TestCase):
    def setUp(self):
        self.goals = ManualGoals()

    def test_add(self):
        goal = self.goals.add('Home Testers', entry(strength='pp', empty_net=True))
        self.assertEqual(goal.id, 'manual-1')
        self.assertEqual((goal.period, goal.clock), (2, '12:28'))
        self.assertEqual(goal.scorer, Player('17', '', ''))
        self.assertEqual(goal.assists, (Player('4', '', ''),))
        self.assertIs(goal.strength, Strength.POWER_PLAY)
        self.assertTrue(goal.empty_net)
        self.assertEqual(self.goals.get('manual-1'), goal)

    def test_blank_assists_are_ignored(self):
        self.assertEqual(self.goals.add('Home Testers', entry(assists=['', ' '])).assists, ())

    def test_invalid_fields(self):
        for fields in ({'period': 0}, {'period': 'x'}, {'clock': '99'}, {'scorer': ''},
                       {'assists': ['4', '9', '11']}, {'strength': 'big'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.goals.add('Home Testers', entry(**fields))
        self.assertEqual(self.goals.goals, [])

    def test_goals_are_in_game_order(self):
        self.goals.add('Home Testers', entry(period=2, clock='05:00'))
        self.goals.add('Home Testers', entry(period=1, clock='03:00'))
        self.goals.add('Home Testers', entry(period=2, clock='15:00'))
        self.assertEqual([goal.id for goal in self.goals.goals], ['manual-2', 'manual-3', 'manual-1'])

    def test_update_keeps_id_and_team(self):
        self.goals.add('Home Testers', entry())
        goal = self.goals.update('manual-1', entry(scorer='4', assists=[]))
        self.assertEqual((goal.id, goal.team_name, goal.scorer.jersey, goal.assists), ('manual-1', 'Home Testers', '4', ()))
        with self.assertRaises(KeyError):
            self.goals.update('manual-9', entry())

    def test_remove(self):
        self.goals.add('Home Testers', entry())
        self.assertIsNotNone(self.goals.remove('manual-1'))
        self.assertIsNone(self.goals.remove('manual-1'))
        self.assertEqual(self.goals.goals, [])

    def test_ids_are_not_reused(self):
        self.goals.add('Home Testers', entry())
        self.goals.remove('manual-1')
        self.assertEqual(self.goals.add('Home Testers', entry()).id, 'manual-2')


class TakeMatchTests(unittest.TestCase):
    def setUp(self):
        self.goals = ManualGoals()

    def test_same_scorer_in_the_period(self):
        self.goals.add('Home Testers', entry(clock='12:28'))
        match = self.goals.take_match('Home Testers', scoresheet_goal(Player('17', 'TESTY', 'MACTESTFACE'), clock='02:00'))
        self.assertEqual(match.id, 'manual-1')
        self.assertEqual(self.goals.goals, [])

    def test_scorer_by_name(self):
        self.goals.add('Home Testers', entry(scorer='MacTestface', clock='19:00'))
        self.assertIsNotNone(self.goals.take_match('Home Testers', scoresheet_goal(Player('17', 'TESTY', 'MACTESTFACE'))))

    def test_different_scorer_close_in_time(self):
        self.goals.add('Home Testers', entry(scorer='4', clock='12:28'))
        self.assertIsNotNone(self.goals.take_match('Home Testers', scoresheet_goal(Player('17', 'TESTY', 'MACTESTFACE'))))

    def test_no_match(self):
        self.goals.add('Home Testers', entry(scorer='4', clock='18:00'))
        testy = Player('17', 'TESTY', 'MACTESTFACE')
        self.assertIsNone(self.goals.take_match('Home Testers', scoresheet_goal(testy)))
        self.assertIsNone(self.goals.take_match('Other Team', scoresheet_goal(Player('4', 'FAKEY', 'FAKERSON'))))
        self.assertIsNone(self.goals.take_match('Home Testers', scoresheet_goal(Player('4', 'FAKEY', 'FAKERSON'), period=3)))
        self.assertEqual(len(self.goals.goals), 1)

    def test_prefers_scorer_then_nearest(self):
        self.goals.add('Home Testers', entry(scorer='4', clock='12:20'))
        self.goals.add('Home Testers', entry(scorer='17', clock='18:00'))
        self.goals.add('Home Testers', entry(scorer='17', clock='10:00'))
        testy = Player('17', 'TESTY', 'MACTESTFACE')
        self.assertEqual(self.goals.take_match('Home Testers', scoresheet_goal(testy, clock='12:20')).id, 'manual-3')


class ManualGoalCaptionTests(unittest.TestCase):
    def test_caption_uses_the_roster(self):
        goal = ManualGoals().add('Home Testers', entry(assists=['4', '9'], strength='shg'))
        self.assertEqual(TEAMS.caption(goal, 'Home Testers'), 'Home Testers Goal: MacTestface (Fakerson, #9) SHG')

    def test_typed_names_are_kept_as_entered(self):
        goal = ManualGoals().add('Visiting Fakes', entry(scorer='22 McFake', assists=[]))
        self.assertEqual(TEAMS.caption(goal, 'Visiting Fakes'), 'Visiting Fakes Goal: McFake (unassisted)')

    def test_list_roster(self):
        self.assertEqual(TEAMS.roster('Home Testers'), {'17': 'MacTestface', '4': 'Fakerson'})
        self.assertEqual(TEAMS.roster('Nobody'), {})


if __name__ == '__main__':
    unittest.main()
