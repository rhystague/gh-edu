from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gh_edu.core import (
    Action,
    ActionType,
    ExecutionLimitError,
    ExecutionPacer,
    ExecutionProgress,
    ExecutionState,
    InputValidationError,
    Plan,
    attach_execution_estimate,
    execution_state_path,
    load_configuration,
    load_execution_state,
    lock_execution_state,
    resolve_invitation_budget,
)
from gh_edu.github import GitHubInvitationLimitError, GitHubRateLimitError, Organisation


class FakeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


def _pacer(
    tmp_path: Path,
    clock: FakeClock,
    *,
    content_limit: int = 450,
    invitation_limit: int = 450,
    wait_for_limits: bool = False,
    progress=None,
    waiting=None,
) -> ExecutionPacer:
    path = tmp_path / "execution-state.json"
    return ExecutionPacer(
        path=path,
        state=ExecutionState(hostname="github.com", organisation="teaching-org"),
        content_limit=content_limit,
        invitation_limit=invitation_limit,
        total_writes=3,
        wait_for_limits=wait_for_limits,
        now=clock.now,
        sleep=clock.sleep,
        progress=progress,
        waiting=waiting,
    )


def test_pacer_waits_exactly_one_second_between_mutation_attempts(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 31, 0, 0, tzinfo=UTC))
    pacer = _pacer(tmp_path, clock)

    pacer.before_write(invitation=False)
    pacer.finish_attempt()
    pacer.before_write(invitation=False)

    assert clock.sleeps == [1.0]
    assert pacer.metrics.pacing_wait_seconds == 1.0
    state = load_execution_state(
        tmp_path / "execution-state.json",
        hostname="github.com",
        organisation="teaching-org",
    )
    assert len(state.content_writes) == 2
    assert not state.invitations


def test_pacer_honours_persisted_attempt_when_a_run_resumes(tmp_path: Path) -> None:
    start = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    path = tmp_path / "execution-state.json"
    state = ExecutionState(
        hostname="github.com",
        organisation="teaching-org",
        content_writes=[start],
    )
    clock = FakeClock(start + timedelta(milliseconds=250))
    pacer = ExecutionPacer(
        path=path,
        state=state,
        content_limit=450,
        invitation_limit=450,
        total_writes=1,
        wait_for_limits=False,
        now=clock.now,
        sleep=clock.sleep,
    )

    pacer.before_write(invitation=False)

    assert clock.sleeps == [0.75]
    assert clock.current == start + timedelta(seconds=1)


def test_hourly_budget_stops_with_next_eligible_timestamp(tmp_path: Path) -> None:
    start = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    clock = FakeClock(start)
    pacer = _pacer(tmp_path, clock, content_limit=1)

    pacer.before_write(invitation=False)
    pacer.finish_attempt()
    with pytest.raises(ExecutionLimitError, match="hourly content-write budget") as raised:
        pacer.before_write(invitation=False)

    assert raised.value.next_eligible_at == start + timedelta(hours=1)
    assert clock.sleeps == [1.0]


def test_wait_mode_sleeps_through_hourly_budget_and_reports_countdown(tmp_path: Path) -> None:
    start = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    clock = FakeClock(start)
    waits: list[tuple[str, datetime, int]] = []
    pacer = _pacer(
        tmp_path,
        clock,
        content_limit=1,
        wait_for_limits=True,
        waiting=lambda reason, resume, remaining: waits.append((reason, resume, remaining)),
    )

    pacer.before_write(invitation=False)
    pacer.finish_attempt()
    pacer.before_write(invitation=False)

    assert clock.current == start + timedelta(hours=1)
    assert pacer.metrics.pacing_wait_seconds == 1
    assert pacer.metrics.limit_wait_seconds == 3599
    assert waits[0][2] == 3599
    assert waits[-1][2] <= 60


def test_invitation_budget_uses_a_rolling_24_hour_window(tmp_path: Path) -> None:
    start = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    clock = FakeClock(start)
    pacer = _pacer(tmp_path, clock, invitation_limit=1)

    pacer.before_write(invitation=True)
    pacer.finish_attempt()
    with pytest.raises(ExecutionLimitError, match="24-hour invitation budget") as raised:
        pacer.before_write(invitation=True)

    assert raised.value.next_eligible_at == start + timedelta(hours=24)


def test_remote_limits_use_retry_after_and_invitation_fallback(tmp_path: Path) -> None:
    start = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    clock = FakeClock(start)
    pacer = _pacer(tmp_path, clock, wait_for_limits=True)

    pacer.handle_remote_limit(
        GitHubRateLimitError("limited", retry_after_seconds=120),
        invitation=False,
    )
    assert clock.current == start + timedelta(minutes=2)

    pacer.handle_remote_limit(
        GitHubInvitationLimitError("invitation endpoint spammed"),
        invitation=True,
    )
    assert clock.current == start + timedelta(minutes=2, hours=24)
    assert pacer.metrics.rate_limit_retries == 2


def test_remote_limit_uses_reset_then_headerless_exponential_backoff(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    reset_clock = FakeClock(start)
    reset_pacer = _pacer(tmp_path, reset_clock, wait_for_limits=True)
    reset_at = int((start + timedelta(minutes=3)).timestamp())

    reset_pacer.handle_remote_limit(
        GitHubRateLimitError("limited", reset_at_epoch=reset_at),
        invitation=False,
    )

    assert reset_clock.current == start + timedelta(minutes=3)

    exponential_clock = FakeClock(start)
    exponential_pacer = _pacer(
        tmp_path,
        exponential_clock,
        wait_for_limits=True,
    )
    exponential_pacer.handle_remote_limit(
        GitHubRateLimitError("limited"),
        invitation=False,
    )
    exponential_pacer.handle_remote_limit(
        GitHubRateLimitError("limited again"),
        invitation=False,
    )
    exponential_pacer.handle_remote_limit(
        GitHubRateLimitError(
            "limited with stale reset",
            reset_at_epoch=int((start - timedelta(minutes=1)).timestamp()),
        ),
        invitation=False,
    )

    assert exponential_clock.sleeps == [60.0] * 7
    assert exponential_clock.current == start + timedelta(minutes=7)


def test_remote_limit_without_wait_consent_returns_reset_time(tmp_path: Path) -> None:
    start = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    clock = FakeClock(start)
    pacer = _pacer(tmp_path, clock)
    reset_at = int((start + timedelta(minutes=5)).timestamp())

    with pytest.raises(ExecutionLimitError) as raised:
        pacer.handle_remote_limit(
            GitHubRateLimitError("limited", reset_at_epoch=reset_at),
            invitation=False,
        )

    assert raised.value.next_eligible_at == start + timedelta(minutes=5)
    assert clock.sleeps == []
    saved = load_execution_state(
        tmp_path / "execution-state.json",
        hostname="github.com",
        organisation="teaching-org",
    )
    assert saved.remote_retry_not_before == start + timedelta(minutes=5)


def test_persisted_remote_cooldown_stops_or_waits_before_writing(tmp_path: Path) -> None:
    start = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    resume_at = start + timedelta(minutes=2)
    path = tmp_path / "execution-state.json"
    state = ExecutionState(
        hostname="github.com",
        organisation="teaching-org",
        remote_retry_not_before=resume_at,
    )
    stopped_clock = FakeClock(start)
    stopped = ExecutionPacer(
        path=path,
        state=state.model_copy(deep=True),
        content_limit=450,
        invitation_limit=450,
        total_writes=1,
        wait_for_limits=False,
        now=stopped_clock.now,
        sleep=stopped_clock.sleep,
    )

    with pytest.raises(ExecutionLimitError) as raised:
        stopped.before_write(invitation=False)

    assert raised.value.next_eligible_at == resume_at
    assert not stopped.state.content_writes

    waiting_clock = FakeClock(start)
    waiting = ExecutionPacer(
        path=path,
        state=state.model_copy(deep=True),
        content_limit=450,
        invitation_limit=450,
        total_writes=1,
        wait_for_limits=True,
        now=waiting_clock.now,
        sleep=waiting_clock.sleep,
    )

    waiting.before_write(invitation=False)

    assert waiting_clock.current == resume_at
    assert waiting_clock.sleeps == [60.0, 60.0]
    saved = load_execution_state(
        path,
        hostname="github.com",
        organisation="teaching-org",
    )
    assert saved.remote_retry_not_before is None
    assert saved.content_writes == [resume_at]


def test_expired_remote_cooldown_is_cleared_without_waiting(tmp_path: Path) -> None:
    current = datetime(2026, 7, 31, 0, 5, tzinfo=UTC)
    clock = FakeClock(current)
    pacer = ExecutionPacer(
        path=tmp_path / "execution-state.json",
        state=ExecutionState(
            hostname="github.com",
            organisation="teaching-org",
            remote_retry_not_before=current - timedelta(minutes=1),
        ),
        content_limit=450,
        invitation_limit=450,
        total_writes=1,
        wait_for_limits=False,
        now=clock.now,
        sleep=clock.sleep,
    )

    pacer.before_write(invitation=False)

    assert clock.sleeps == []
    assert pacer.state.remote_retry_not_before is None


def test_interrupted_remote_limit_wait_preserves_cooldown(tmp_path: Path) -> None:
    start = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    path = tmp_path / "execution-state.json"

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    pacer = ExecutionPacer(
        path=path,
        state=ExecutionState(hostname="github.com", organisation="teaching-org"),
        content_limit=450,
        invitation_limit=450,
        total_writes=1,
        wait_for_limits=True,
        now=lambda: start,
        sleep=interrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        pacer.handle_remote_limit(
            GitHubRateLimitError("limited", retry_after_seconds=120),
            invitation=False,
        )

    saved = load_execution_state(
        path,
        hostname="github.com",
        organisation="teaching-org",
    )
    assert saved.remote_retry_not_before == start + timedelta(minutes=2)


def test_progress_callback_contains_only_aggregate_status(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 31, 0, 0, tzinfo=UTC))
    progress: list[ExecutionProgress] = []
    pacer = _pacer(tmp_path, clock, progress=progress.append)

    pacer.finish_action(phase="create repository", succeeded=True)
    pacer.finish_action(phase="send invitation", succeeded=False)

    assert [(item.processed, item.successful, item.failed) for item in progress] == [
        (1, 1, 0),
        (2, 1, 1),
    ]
    assert all("@" not in item.phase for item in progress)


def test_automatic_invitation_budget_uses_age_plan_and_override(config_factory) -> None:
    current = datetime(2026, 7, 31, tzinfo=UTC)
    config = load_configuration(config_factory())
    old_free = Organisation("teaching-org", "2020-01-01T00:00:00Z", "free")
    new_free = Organisation("teaching-org", "2026-07-15T00:00:00Z", "free")
    new_paid = Organisation("teaching-org", "2026-07-15T00:00:00Z", "team")

    assert resolve_invitation_budget(config, old_free, now=current) == 450
    assert resolve_invitation_budget(config, new_free, now=current) == 45
    assert resolve_invitation_budget(config, new_paid, now=current) == 450

    overridden = load_configuration(
        config_factory(overrides={"execution": {"invitation_budget_per_24_hours": 300}})
    )
    assert resolve_invitation_budget(overridden, new_free, now=current) == 300


@pytest.mark.parametrize("value", [0, 501, True, "500"])
def test_invalid_invitation_budget_is_rejected(config_factory, value) -> None:
    with pytest.raises(InputValidationError, match="invitation_budget_per_24_hours"):
        load_configuration(
            config_factory(overrides={"execution": {"invitation_budget_per_24_hours": value}})
        )


@pytest.mark.parametrize("value", [0, 451, True, "450"])
def test_invalid_content_write_budget_is_rejected(config_factory, value) -> None:
    with pytest.raises(InputValidationError, match="content_writes_per_hour"):
        load_configuration(
            config_factory(overrides={"execution": {"content_writes_per_hour": value}})
        )


def test_execution_state_path_validation_and_locking(config_factory) -> None:
    config_path = config_factory()
    config = load_configuration(config_path)
    path = execution_state_path(config_path, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"schema_version":1,"hostname":"github.com","organisation":"other-org",'
        '"content_writes":[],"invitations":[]}\n',
        encoding="utf-8",
    )

    with pytest.raises(InputValidationError, match="belongs to organisation"):
        load_execution_state(path, hostname="github.com", organisation="teaching-org")

    path.unlink()
    with (
        lock_execution_state(path),
        pytest.raises(InputValidationError, match="Another gh-edu apply"),
        lock_execution_state(path),
    ):
        pass


def test_version_one_execution_state_without_remote_cooldown_still_loads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-state.json"
    path.write_text(
        '{"schema_version":1,"hostname":"github.com",'
        '"organisation":"teaching-org","content_writes":[],"invitations":[]}\n',
        encoding="utf-8",
    )

    state = load_execution_state(
        path,
        hostname="github.com",
        organisation="teaching-org",
    )

    assert state.remote_retry_not_before is None


def test_execution_state_rejects_naive_remote_cooldown(tmp_path: Path) -> None:
    path = tmp_path / "execution-state.json"
    path.write_text(
        '{"schema_version":1,"hostname":"github.com",'
        '"organisation":"teaching-org","content_writes":[],"invitations":[],'
        '"remote_retry_not_before":"2026-07-31T12:00:00"}\n',
        encoding="utf-8",
    )

    with pytest.raises(InputValidationError, match="must include a timezone"):
        load_execution_state(
            path,
            hostname="github.com",
            organisation="teaching-org",
        )


def test_execution_state_prunes_old_timestamps_and_saves_atomically(
    tmp_path: Path,
) -> None:
    current = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    path = tmp_path / "execution-state.json"
    state = ExecutionState(
        hostname="github.com",
        organisation="teaching-org",
        content_writes=[
            current - timedelta(hours=2),
            current - timedelta(minutes=5),
        ],
        invitations=[
            current - timedelta(days=2),
            current - timedelta(hours=2),
        ],
    )
    clock = FakeClock(current)
    pacer = ExecutionPacer(
        path=path,
        state=state,
        content_limit=450,
        invitation_limit=450,
        total_writes=1,
        wait_for_limits=False,
        now=clock.now,
        sleep=clock.sleep,
    )

    pacer.before_write(invitation=False)

    saved = load_execution_state(
        path,
        hostname="github.com",
        organisation="teaching-org",
    )
    assert saved.content_writes == [current - timedelta(minutes=5), current]
    assert saved.invitations == [current - timedelta(hours=2)]
    assert not list(tmp_path.glob("*.tmp"))


def test_execution_estimate_reports_hourly_and_daily_windows(config_factory) -> None:
    config = load_configuration(
        config_factory(
            overrides={
                "execution": {
                    "content_writes_per_hour": 2,
                    "invitation_budget_per_24_hours": 1,
                }
            }
        )
    )
    actions = [
        Action(
            action_id=f"invite:{index}",
            action_type=ActionType.SEND_INVITATION,
            scope="test",
            reason="test",
        )
        for index in range(3)
    ]
    plan = Plan(
        title="Test",
        organisation="teaching-org",
        subject="COMP3018",
        term="2026S2",
        mode="Dry run",
        generated_at=datetime(2026, 7, 31, tzinfo=UTC),
        actions=actions,
    )

    attach_execution_estimate(plan, config, None)

    assert plan.execution_estimate is not None
    assert plan.execution_estimate.content_windows == 2
    assert plan.execution_estimate.invitation_windows == 3
    assert plan.execution_estimate.minimum_seconds == 172800
    assert len(plan.warnings) == 2
