"""Small, framework-independent state machine for hockey penalties."""

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional


class Team(str, Enum):
    HOME = "home"
    AWAY = "away"


class PenaltyType(str, Enum):
    MINOR = "minor"
    DOUBLE_MINOR = "double_minor"
    MAJOR = "major"


class PenaltyStatus(str, Enum):
    ACTIVE = "active"
    QUEUED = "queued"
    SERVED = "served"
    CANCELLED = "cancelled"


DEFAULT_DURATIONS = {
    PenaltyType.MINOR: 120,
    PenaltyType.DOUBLE_MINOR: 240,
    PenaltyType.MAJOR: 300,
}
MINOR_SEGMENT_SECONDS = 120


@dataclass(frozen=True)
class PenaltyAssessment:
    team: Team
    player: str
    kind: PenaltyType = PenaltyType.MINOR
    duration_seconds: Optional[float] = None
    offset_group: Optional[str] = None


@dataclass
class Penalty:
    id: int
    team: Team
    player: str
    kind: PenaltyType
    duration_seconds: float
    remaining_seconds: float
    status: PenaltyStatus
    offset_group: Optional[str] = None
    segments_remaining: int = 1

    @property
    def is_offset(self) -> bool:
        return self.offset_group is not None


class PenaltyStateMachine:
    """Tracks penalty clocks and the resulting skater advantage.

    Penalty clocks advance only when ``advance`` is called, so the host app
    controls whether its game clock is running. Offset calls are assessed
    together with a shared offset group and do not change manpower.
    """

    MAX_PENALTY_ADVANTAGE = 2
    MIN_SKATERS = 3

    def __init__(self, base_skaters: int = 5) -> None:
        if base_skaters < self.MIN_SKATERS:
            raise ValueError("base_skaters cannot be less than three")
        self.base_skaters = base_skaters
        self.penalties: list[Penalty] = []
        self._next_id = 1

    def assess_penalty(
        self,
        team: Team,
        player: str,
        kind: PenaltyType = PenaltyType.MINOR,
        duration_seconds: Optional[float] = None,
    ) -> Penalty:
        return self.assess_penalties([
            PenaltyAssessment(team, player, kind, duration_seconds)
        ])[0]

    def assess_penalties(
        self, assessments: Iterable[PenaltyAssessment]
    ) -> list[Penalty]:
        assessments = list(assessments)
        offset_groups: dict[str, list[PenaltyAssessment]] = {}
        for assessment in assessments:
            if assessment.offset_group:
                offset_groups.setdefault(assessment.offset_group, []).append(assessment)

        for group, members in offset_groups.items():
            teams = [self._team(member.team) for member in members]
            durations = [self._duration(member) for member in members]
            if len(members) != 2 or set(teams) != {Team.HOME, Team.AWAY}:
                raise ValueError(f"offset group {group!r} must contain one penalty per team")
            if durations[0] != durations[1]:
                raise ValueError(f"offset group {group!r} penalties must have equal durations")

        assessed = []
        for assessment in assessments:
            team = self._team(assessment.team)
            kind = self._kind(assessment.kind)
            duration = self._duration(assessment)
            penalty = Penalty(
                id=self._next_id,
                team=team,
                player=assessment.player,
                kind=kind,
                duration_seconds=duration,
                remaining_seconds=duration,
                status=PenaltyStatus.ACTIVE,
                offset_group=assessment.offset_group,
                segments_remaining=2 if kind is PenaltyType.DOUBLE_MINOR else 1,
            )
            self._next_id += 1
            if not penalty.is_offset and self._active_advantage_count(team) >= self.MAX_PENALTY_ADVANTAGE:
                penalty.status = PenaltyStatus.QUEUED
            self.penalties.append(penalty)
            assessed.append(penalty)
        return assessed

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("seconds cannot be negative")
        remaining = float(seconds)
        while remaining > 0:
            active = [p for p in self.penalties if p.status is PenaltyStatus.ACTIVE]
            if not active:
                break
            step = min(remaining, min(p.remaining_seconds for p in active))
            for penalty in active:
                penalty.remaining_seconds = max(0.0, penalty.remaining_seconds - step)
                if penalty.remaining_seconds == 0:
                    penalty.status = PenaltyStatus.SERVED
                    penalty.segments_remaining = 0
                elif penalty.kind is PenaltyType.DOUBLE_MINOR:
                    penalty.segments_remaining = 2 if penalty.remaining_seconds > MINOR_SEGMENT_SECONDS else 1
            remaining -= step
            self._activate_queued(Team.HOME)
            self._activate_queued(Team.AWAY)

    def record_goal(self, scoring_team: Team) -> Optional[Penalty]:
        """Cancel the oldest active opponent minor segment on a power-play goal."""
        scoring_team = self._team(scoring_team)
        if self.skaters_for(scoring_team) <= self.skaters_for(self._opponent(scoring_team)):
            return None
        opponent = self._opponent(scoring_team)
        candidates = [
            penalty for penalty in self.penalties
            if penalty.team is opponent
            and penalty.kind in (PenaltyType.MINOR, PenaltyType.DOUBLE_MINOR)
            and penalty.status is PenaltyStatus.ACTIVE
            and not penalty.is_offset
        ]
        if not candidates:
            return None
        penalty = min(
            candidates,
            key=lambda item: (
                item.kind is PenaltyType.DOUBLE_MINOR and item.segments_remaining == 1,
                item.id,
            ),
        )
        if penalty.kind is PenaltyType.DOUBLE_MINOR and penalty.segments_remaining > 1:
            penalty.remaining_seconds = float(MINOR_SEGMENT_SECONDS)
            penalty.segments_remaining = 1
            return penalty
        penalty.status = PenaltyStatus.CANCELLED
        penalty.remaining_seconds = 0
        penalty.segments_remaining = 0
        self._activate_queued(opponent)
        return penalty

    def skaters_for(self, team: Team) -> int:
        team = self._team(team)
        advantage_count = self._active_advantage_count(team)
        return max(self.MIN_SKATERS, self.base_skaters - advantage_count)

    @property
    def strength(self) -> str:
        return f"{self.skaters_for(Team.HOME)}-on-{self.skaters_for(Team.AWAY)}"

    @property
    def power_play_team(self) -> Optional[Team]:
        home = self.skaters_for(Team.HOME)
        away = self.skaters_for(Team.AWAY)
        if home == away:
            return None
        return Team.HOME if home > away else Team.AWAY

    @property
    def power_play_time_remaining(self) -> Optional[int]:
        advantaged_team = self.power_play_team
        if advantaged_team is None:
            return None
        opponent = self._opponent(advantaged_team)
        active = [
            p.remaining_seconds for p in self.penalties
            if p.team is opponent
            and p.status is PenaltyStatus.ACTIVE
            and not p.is_offset
        ]
        return int(min(active)) if active else None

    def active_penalties(self, team: Optional[Team] = None) -> list[Penalty]:
        normalized_team = self._team(team) if team is not None else None
        return [
            penalty for penalty in self.penalties
            if penalty.status in (PenaltyStatus.ACTIVE, PenaltyStatus.QUEUED)
            and (normalized_team is None or penalty.team is normalized_team)
        ]

    def _active_advantage_count(self, team: Team) -> int:
        return sum(
            1 for penalty in self.penalties
            if penalty.team is team
            and penalty.status is PenaltyStatus.ACTIVE
            and not penalty.is_offset
        )

    def _activate_queued(self, team: Team) -> None:
        queued = sorted(
            (p for p in self.penalties if p.team is team and p.status is PenaltyStatus.QUEUED),
            key=lambda item: item.id,
        )
        for penalty in queued:
            if self._active_advantage_count(team) >= self.MAX_PENALTY_ADVANTAGE:
                break
            penalty.status = PenaltyStatus.ACTIVE

    @staticmethod
    def _team(team: Team) -> Team:
        return team if isinstance(team, Team) else Team(team)

    @staticmethod
    def _kind(kind: PenaltyType) -> PenaltyType:
        return kind if isinstance(kind, PenaltyType) else PenaltyType(kind)

    @staticmethod
    def _opponent(team: Team) -> Team:
        return Team.AWAY if team is Team.HOME else Team.HOME

    @staticmethod
    def _duration(assessment: PenaltyAssessment) -> float:
        duration = assessment.duration_seconds
        kind = PenaltyStateMachine._kind(assessment.kind)
        if duration is None:
            duration = DEFAULT_DURATIONS[kind]
        if duration <= 0:
            raise ValueError("penalty duration must be positive")
        if kind is PenaltyType.DOUBLE_MINOR and duration != DEFAULT_DURATIONS[kind]:
            raise ValueError("double minor duration must be 240 seconds")
        return float(duration)