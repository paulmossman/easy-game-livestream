import unittest

from src.scoresheets import Goal, Player, Strength
from src.goal_captions import Teams, capitalize_name

TESTY = Player('17', 'TESTY', 'MACTESTFACE')
FAKEY = Player('4', 'FAKEY', 'FAKERSON')
SAMPLE = Player('9', 'SAMPLE', "O'SKATERSON-SMITH")
OPPONENT = Player('17', 'NOTREAL', 'PERSONNAME')

TEAMS_CONFIG = [
    {'name': 'Home Testers', 'roster': {17: 'MacTestface', '04': 'Fakerson'}},
    {'name': 'Visiting Fakes', 'celebrate': True},
]


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
        self.assertFalse(self.teams.celebrate('Unknown Team'))
        quiet = Teams([{'name': 'Quiet Testers', 'celebrate': False, 'roster': {1: 'Goalieguy'}}])
        self.assertFalse(quiet.celebrate('Quiet Testers'))

    def test_no_teams_config(self):
        teams = Teams(None)
        self.assertFalse(teams.celebrate('Home Testers'))
        self.assertEqual(teams.player_name(TESTY), 'Testy Mactestface')

    def test_team_without_settings(self):
        self.assertFalse(Teams([{'name': 'Home Testers'}]).celebrate('Home Testers'))

    def test_roster_match_needs_jersey_and_surname(self):
        self.assertEqual(self.teams.roster_match(TESTY), ('Home Testers', 'MacTestface'))
        self.assertEqual(self.teams.roster_match(FAKEY), ('Home Testers', 'Fakerson'))
        self.assertIsNone(self.teams.roster_match(OPPONENT))
        self.assertIsNone(self.teams.roster_match(Player('18', 'TESTY', 'MACTESTFACE')))

    def test_roster_match_on_text_containing_the_surname(self):
        teams = Teams([{'name': 'Home Testers', 'roster': {17: 'Testy MacTestface'}}])
        self.assertEqual(teams.roster_match(TESTY), ('Home Testers', 'Testy MacTestface'))

    def test_player_name(self):
        self.assertEqual(self.teams.player_name(TESTY), 'MacTestface')
        self.assertEqual(self.teams.player_name(SAMPLE), "Sample O'Skaterson-Smith")
        self.assertEqual(self.teams.player_name(Player('', 'TESTY', '')), 'Testy')

    def test_player_name_uses_the_scoring_teams_roster_by_number(self):
        nickname = Teams([{'name': 'Home Testers', 'roster': {17: 'Testy T'}}])
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


class TeamsConfigTests(unittest.TestCase):
    def test_list_order_without_overlay_position(self):
        teams = Teams(TEAMS_CONFIG)
        self.assertEqual(teams.overlay_name('left'), 'Home Testers')
        self.assertEqual(teams.overlay_name('right'), 'Visiting Fakes')
        self.assertEqual(teams.warnings, [])

    def test_overlay_position_on_one_team_places_the_other(self):
        teams = Teams([{'name': 'Home Testers'}, {'name': 'Visiting Fakes', 'overlay_position': 'Left'}])
        self.assertEqual(teams.overlay_name('left'), 'Visiting Fakes')
        self.assertEqual(teams.overlay_name('right'), 'Home Testers')

    def test_full_name_defaults_to_the_name(self):
        teams = Teams([{'name': 'Testers', 'full_name': 'Testville Junior Testers'}, {'name': 'Fakes', 'full_name': ' '}])
        self.assertEqual(teams.full_name('Testers'), 'Testville Junior Testers')
        self.assertEqual(teams.full_name('Fakes'), 'Fakes')
        # A name typed in the UI that isn't in the config.
        self.assertEqual(teams.full_name('Renamed Testers'), 'Renamed Testers')
        self.assertEqual(teams.overlay_name('left'), 'Testers')

    def test_one_team(self):
        teams = Teams([{'name': 'Home Testers', 'overlay_position': 'right'}])
        self.assertIsNone(teams.overlay_name('left'))
        self.assertEqual(teams.overlay_name('right'), 'Home Testers')

    def test_no_teams(self):
        self.assertIsNone(Teams(None).overlay_name('left'))
        self.assertEqual(Teams(None).warnings, [])

    def test_same_overlay_position_twice(self):
        teams = Teams([{'name': 'Home Testers', 'overlay_position': 'right'},
                       {'name': 'Visiting Fakes', 'overlay_position': 'right'}])
        self.assertEqual(teams.overlay_name('right'), 'Home Testers')
        self.assertEqual(teams.overlay_name('left'), 'Visiting Fakes')
        self.assertEqual(len(teams.warnings), 1)

    def test_invalid_overlay_position(self):
        teams = Teams([{'name': 'Home Testers', 'overlay_position': 'middle'}, {'name': 'Visiting Fakes'}])
        self.assertEqual(teams.overlay_name('left'), 'Home Testers')
        self.assertIn('overlay_position', teams.warnings[0])

    def test_same_scoresheet_side_twice(self):
        teams = Teams([{'name': 'Home Testers', 'scoresheet': 'home'}, {'name': 'Visiting Fakes', 'scoresheet': 'home'}])
        self.assertEqual(teams.side_teams([], 'Home Testers', 'Visiting Fakes'),
                         {'home': 'Home Testers', 'away': 'Visiting Fakes'})
        self.assertEqual(len(teams.warnings), 1)

    def test_invalid_scoresheet_side(self):
        teams = Teams([{'name': 'Home Testers', 'scoresheet': 'guest'}])
        self.assertIn('scoresheet', teams.warnings[0])

    def test_old_mapping_format_is_ignored(self):
        teams = Teams({'Home Testers': {'roster': {17: 'MacTestface'}}})
        self.assertIsNone(teams.overlay_name('left'))
        self.assertEqual(teams.roster('Home Testers'), {})
        self.assertIn('must be a list', teams.warnings[0])

    def test_entries_without_a_name_repeats_and_a_third_team_are_ignored(self):
        teams = Teams([{'roster': {17: 'MacTestface'}}, 'Home Testers', {'name': 'Home Testers'},
                       {'name': 'Home Testers'}, {'name': 'Visiting Fakes'}, {'name': 'Third Fakes'}])
        self.assertEqual(teams.overlay_name('left'), 'Home Testers')
        self.assertEqual(teams.overlay_name('right'), 'Visiting Fakes')
        self.assertEqual(teams.roster('Third Fakes'), {})
        self.assertEqual(len(teams.warnings), 4)


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
        teams = Teams([{'name': 'Home Testers', 'scoresheet': 'away'}, {'name': 'Visiting Fakes', 'scoresheet': 'Home'}])
        sides = teams.side_teams([goal('away', OPPONENT)], 'Home Testers', 'Visiting Fakes')
        self.assertEqual(sides, {'home': 'Visiting Fakes', 'away': 'Home Testers'})

    def test_scoresheet_setting_names_the_opponent_too(self):
        teams = Teams([{'name': 'Home Testers', 'scoresheet': 'visitor'}])
        self.assertEqual(teams.side_teams([], 'Home Testers', 'Visiting Fakes'),
                         {'home': 'Visiting Fakes', 'away': 'Home Testers'})

    def test_scoresheet_setting_beats_the_roster(self):
        config = [{'name': 'Home Testers', 'roster': {17: 'MacTestface'}, 'scoresheet': 'away'}, TEAMS_CONFIG[1]]
        sides = Teams(config).side_teams([goal('home', TESTY)], 'Home Testers', 'Visiting Fakes')
        self.assertEqual(sides, {'home': 'Visiting Fakes', 'away': 'Home Testers'})

    def test_most_matches_wins(self):
        # An opponent who happens to share a jersey number and surname.
        sides = self.teams.side_teams(
            [goal('home', TESTY, [FAKEY]), goal('away', Player('4', 'OTHER', 'FAKERSON'))],
            'Home Testers', 'Visiting Fakes')
        self.assertEqual(sides['home'], 'Home Testers')



class GoalTextTests(unittest.TestCase):
    def test_goal_text_is_the_caption_without_the_team(self):
        teams = Teams(TEAMS_CONFIG)
        scored = goal('home', TESTY, [FAKEY], strength=Strength.POWER_PLAY)
        self.assertEqual(teams.goal_text(scored, 'Home Testers'), 'MacTestface (Fakerson) PP')
        self.assertEqual(teams.caption(scored, 'Home Testers'), 'Home Testers Goal: MacTestface (Fakerson) PP')
        self.assertEqual(teams.goal_text(scored, 'Home Testers', settled=False), 'MacTestface (Fakerson, ...) PP')

if __name__ == '__main__':
    unittest.main()
