import unittest

from src.penalties import (
    PenaltyAssessment,
    PenaltyStateMachine,
    PenaltyStatus,
    PenaltyType,
    Team,
)


class PenaltyStateMachineTests(unittest.TestCase):
    def test_one_penalty_each_side_is_four_on_four(self):
        game = PenaltyStateMachine()
        game.assess_penalty(Team.HOME, "H1")
        game.assess_penalty(Team.AWAY, "A1")

        self.assertEqual(game.strength, "4-on-4")
        self.assertIsNone(game.power_play_team)

    def test_offsetting_penalties_do_not_change_manpower(self):
        game = PenaltyStateMachine()
        game.assess_penalties([
            PenaltyAssessment(Team.HOME, "H1", offset_group="fight-1"),
            PenaltyAssessment(Team.AWAY, "A1", offset_group="fight-1"),
        ])

        self.assertEqual(game.strength, "5-on-5")
        game.advance(30)
        self.assertEqual(game.strength, "5-on-5")

    def test_queued_penalty_starts_and_ticks_after_first_expires(self):
        game = PenaltyStateMachine()
        first, second, queued = game.assess_penalties([
            PenaltyAssessment(Team.AWAY, "A1", duration_seconds=10),
            PenaltyAssessment(Team.AWAY, "A2", duration_seconds=20),
            PenaltyAssessment(Team.AWAY, "A3", duration_seconds=30),
        ])

        self.assertIs(queued.status, PenaltyStatus.QUEUED)
        game.advance(15)

        self.assertIs(first.status, PenaltyStatus.SERVED)
        self.assertEqual(second.remaining_seconds, 5)
        self.assertIs(queued.status, PenaltyStatus.ACTIVE)
        self.assertEqual(queued.remaining_seconds, 25)

    def test_power_play_goal_cancels_minor_but_not_major(self):
        game = PenaltyStateMachine()
        major, minor = game.assess_penalties([
            PenaltyAssessment(Team.AWAY, "A1", PenaltyType.MAJOR),
            PenaltyAssessment(Team.AWAY, "A2", PenaltyType.MINOR),
        ])

        cancelled = game.record_goal(Team.HOME)

        self.assertIs(cancelled, minor)
        self.assertIs(minor.status, PenaltyStatus.CANCELLED)
        self.assertIs(major.status, PenaltyStatus.ACTIVE)
        self.assertEqual(game.strength, "5-on-4")

    def test_goal_during_first_double_minor_segment_starts_second_segment(self):
        game = PenaltyStateMachine()
        double_minor = game.assess_penalty(Team.AWAY, "A1", PenaltyType.DOUBLE_MINOR)
        game.advance(30)

        self.assertIs(game.record_goal(Team.HOME), double_minor)

        self.assertIs(double_minor.status, PenaltyStatus.ACTIVE)
        self.assertEqual(double_minor.remaining_seconds, 120)
        self.assertEqual(double_minor.segments_remaining, 1)
        self.assertEqual(game.strength, "5-on-4")

    def test_goal_during_second_double_minor_segment_cancels_it(self):
        game = PenaltyStateMachine()
        double_minor = game.assess_penalty(Team.AWAY, "A1", PenaltyType.DOUBLE_MINOR)
        game.advance(120)

        self.assertEqual(double_minor.segments_remaining, 1)
        self.assertIs(game.record_goal(Team.HOME), double_minor)

        self.assertIs(double_minor.status, PenaltyStatus.CANCELLED)
        self.assertEqual(game.strength, "5-on-5")

    def test_double_minor_and_later_minor_keep_distinct_players(self):
        game = PenaltyStateMachine()
        double_minor = game.assess_penalty(Team.AWAY, "A1", PenaltyType.DOUBLE_MINOR)
        later_minor = game.assess_penalty(Team.AWAY, "A2", PenaltyType.MINOR)

        self.assertNotEqual(double_minor.id, later_minor.id)
        self.assertEqual(double_minor.player, "A1")
        self.assertEqual(later_minor.player, "A2")
        self.assertEqual(game.strength, "5-on-3")
        game.record_goal(Team.HOME)
        self.assertIs(double_minor.status, PenaltyStatus.ACTIVE)
        self.assertEqual(double_minor.remaining_seconds, 120)
        self.assertIs(later_minor.status, PenaltyStatus.ACTIVE)

    def test_goal_cancels_later_minor_before_second_double_minor_segment(self):
        game = PenaltyStateMachine()
        double_minor = game.assess_penalty(Team.AWAY, "A1", PenaltyType.DOUBLE_MINOR)
        game.advance(20)
        game.record_goal(Team.HOME)

        game.advance(10)
        single_minor = game.assess_penalty(Team.AWAY, "A2", PenaltyType.MINOR)
        game.advance(10)
        cancelled = game.record_goal(Team.HOME)

        self.assertIs(cancelled, single_minor)
        self.assertIs(single_minor.status, PenaltyStatus.CANCELLED)
        self.assertIs(double_minor.status, PenaltyStatus.ACTIVE)
        self.assertEqual(double_minor.segments_remaining, 1)
        self.assertEqual(double_minor.remaining_seconds, 100)

    def test_short_handed_goal_does_not_cancel_opponent_minor(self):
        game = PenaltyStateMachine()
        game.assess_penalties([
            PenaltyAssessment(Team.HOME, "H1"),
            PenaltyAssessment(Team.HOME, "H2"),
        ])
        minor = game.assess_penalty(Team.AWAY, "A1")

        self.assertEqual(game.strength, "3-on-4")
        self.assertIsNone(game.record_goal(Team.HOME))
        self.assertIs(minor.status, PenaltyStatus.ACTIVE)

    def test_clock_expiry_restores_strength(self):
        game = PenaltyStateMachine()
        penalty = game.assess_penalty(Team.AWAY, "A1", duration_seconds=12)
        game.advance(12)

        self.assertIs(penalty.status, PenaltyStatus.SERVED)
        self.assertEqual(game.strength, "5-on-5")


if __name__ == "__main__":
    unittest.main()