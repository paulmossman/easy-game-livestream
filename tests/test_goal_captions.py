import unittest

from src.scoresheets import Goal, Player, Strength
from src.goal_captions import Teams, capitalize_name

TESTY = Player('17', 'TESTY', 'MACTESTFACE')
FAKEY = Player('4', 'FAKEY', 'FAKERSON')
SAMPLE = Player('9', 'SAMPLE', "O'SKATERSON-SMITH")
OPPONENT = Player('17', 'NOTREAL', 'PERSONNAME')

TEAMS_CONFIG = {
    'Home Testers': {'roster': {17: 'MacTestface', '04': 'Fakerson'}},
    'Visiting Fakes': {'celebrate': True},
    'Quiet Testers': {'celebrate': False, 'roster': {1: 'Goalieguy'}},
}


def goal(team, scorer, assists=(), strength=Strength.EVEN, empty_net=False, goal_id='g1'):
    return Goal(goal_id, 1, '10:00', team, scorer, tuple(assists), strength, empty_net)


class CapitalizeNameTests(unittest.TestCase):
    def test_capitalize_name(self):
        self.assertEqual(capitalize_name('MACDONALD'), 'Macdonald')
        self.assertEqual(capitalize_name("O'BRIEN-SMITH"), "O'Brien-Smith")
        self.assertEqual(capitalize_name('VAN DER FAKE'), 'Van Der Fake')
        self.assertEqual(capitalize_name(''), '')


class TeamsTests(unittest.TestCase):
    def setUp(self):
        self.teams = Teams(TEAMS_CONFIG)

    def test_celebrate_defaults_to_having_a_roster(self):
        self.assertTrue(self.teams.celebrate('Home Testers'))
        self.assertTrue(self.teams.celebrate('Visiting Fakes'))
        self.assertFalse(self.teams.celebrate('Quiet Testers'))
        self.assertFalse(self.teams.celebrate('Unknown Team'))

    def test_no_teams_config(self):
        teams = Teams(None)
        self.assertFalse(teams.celebrate('Home Testers'))
        self.assertEqual(teams.player_name(TESTY), 'Testy Mactestface')

    def test_team_without_settings(self):
        self.assertFalse(Teams({'Home Testers': None}).celebrate('Home Testers'))

    def test_roster_match_needs_jersey_and_surname(self):
        self.assertEqual(self.teams.roster_match(TESTY), ('Home Testers', 'MacTestface'))
        self.assertEqual(self.teams.roster_match(FAKEY), ('Home Testers', 'Fakerson'))
        self.assertIsNone(self.teams.roster_match(OPPONENT))
        self.assertIsNone(self.teams.roster_match(Player('18', 'TESTY', 'MACTESTFACE')))

    def test_roster_match_on_text_containing_the_surname(self):
        teams = Teams({'Home Testers': {'roster': {17: 'Testy MacTestface'}}})
        self.assertEqual(teams.roster_match(TESTY), ('Home Testers', 'Testy MacTestface'))

    def test_player_name(self):
        self.assertEqual(self.teams.player_name(TESTY), 'MacTestface')
        self.assertEqual(self.teams.player_name(SAMPLE), "Sample O'Skaterson-Smith")
        self.assertEqual(self.teams.player_name(Player('', 'TESTY', '')), 'Testy')

    def test_player_name_uses_the_scoring_teams_roster_by_number(self):
        nickname = Teams({'Home Testers': {'roster': {17: 'Testy T'}}})
        self.assertEqual(nickname.player_name(TESTY, 'Home Testers'), 'Testy T')
        # Not on that team's roster: the scoresheet name.
        self.assertEqual(self.teams.player_name(SAMPLE, 'Home Testers'), "Sample O'Skaterson-Smith")
        # Another team's roster isn't used just because the number matches.
        self.assertEqual(self.teams.player_name(OPPONENT, 'Visiting Fakes'), 'Notreal Personname')

    def test_caption(self):
        self.assertEqual(
            self.teams.caption(goal('home', TESTY, [FAKEY, SAMPLE], Strength.POWER_PLAY), 'Home Testers'),
            "Home Testers Goal: MacTestface (Fakerson, Sample O'Skaterson-Smith) PP",
        )
        self.assertEqual(
            self.teams.caption(goal('away', OPPONENT, strength=Strength.SHORTHANDED, empty_net=True), 'Visiting Fakes'),
            'Visiting Fakes Goal: Notreal Personname (unassisted) SHG EN',
        )

    def test_caption_waits_for_assists_until_settled(self):
        self.assertEqual(
            self.teams.caption(goal('home', TESTY), 'Home Testers', settled=False),
            'Home Testers Goal: MacTestface (...)',
        )
        self.assertEqual(
            self.teams.caption(goal('home', TESTY, [FAKEY]), 'Home Testers', settled=False),
            'Home Testers Goal: MacTestface (Fakerson, ...)',
        )
        self.assertEqual(
            self.teams.caption(goal('home', TESTY, [FAKEY, SAMPLE]), 'Home Testers', settled=False),
            "Home Testers Goal: MacTestface (Fakerson, Sample O'Skaterson-Smith)",
        )


class SideTeamsTests(unittest.TestCase):
    def setUp(self):
        self.teams = Teams(TEAMS_CONFIG)

    def test_unknown_before_a_roster_match(self):
        sides = self.teams.side_teams([goal('away', OPPONENT)], 'Home Testers', 'Visiting Fakes')
        self.assertEqual(sides, {'home': None, 'away': None})

    def test_roster_match_names_both_sides(self):
        sides = self.teams.side_teams([goal('away', OPPONENT), goal('home', SAMPLE, [FAKEY])],
                                      'Visiting Fakes', 'Home Testers')
        self.assertEqual(sides, {'home': 'Home Testers', 'away': 'Visiting Fakes'})

    def test_same_name_on_both_sides_leaves_opponent_unknown(self):
        sides = self.teams.side_teams([goal('away', TESTY)], 'Home Testers', 'Home Testers')
        self.assertEqual(sides, {'home': None, 'away': 'Home Testers'})

    def test_scoresheet_setting_names_the_side(self):
        teams = Teams({'Home Testers': {'scoresheet': 'away'}, 'Visiting Fakes': {'scoresheet': 'Home'}})
        sides = teams.side_teams([goal('away', OPPONENT)], 'Home Testers', 'Visiting Fakes')
        self.assertEqual(sides, {'home': 'Visiting Fakes', 'away': 'Home Testers'})

    def test_scoresheet_setting_names_the_opponent_too(self):
        teams = Teams({'Home Testers': {'scoresheet': 'visitor'}})
        self.assertEqual(teams.side_teams([], 'Home Testers', 'Visiting Fakes'),
                         {'home': 'Visiting Fakes', 'away': 'Home Testers'})

    def test_scoresheet_setting_beats_the_roster(self):
        config = dict(TEAMS_CONFIG, **{'Home Testers': {'roster': {17: 'MacTestface'}, 'scoresheet': 'away'}})
        sides = Teams(config).side_teams([goal('home', TESTY)], 'Home Testers', 'Visiting Fakes')
        self.assertEqual(sides, {'home': 'Visiting Fakes', 'away': 'Home Testers'})

    def test_most_matches_wins(self):
        # An opponent who happens to share a jersey number and surname.
        sides = self.teams.side_teams(
            [goal('home', TESTY, [FAKEY]), goal('away', Player('4', 'OTHER', 'FAKERSON'))],
            'Home Testers', 'Visiting Fakes')
        self.assertEqual(sides['home'], 'Home Testers')


if __name__ == '__main__':
    unittest.main()
