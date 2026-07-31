from __future__ import annotations

from pathlib import Path

import pytest

import gh_edu.core as core
from gh_edu.core import (
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    EXIT_UNEXPECTED,
    ActionStatus,
    ActionType,
    DesiredGroup,
    DesiredRepository,
    InputValidationError,
    InvitationLedger,
    InvitationState,
    Snapshot,
    Student,
    build_group_resources,
    build_provision_plan,
    discover_snapshot,
    execute_plan,
    ledger_path,
    load_configuration,
    load_ledger,
    load_roster,
    reconcile_invitation,
)
from gh_edu.github import (
    FailedInvitation,
    GitHubError,
    GitHubNetworkError,
    Repository,
    Team,
    TeamMember,
)


def _ready_group(config_path: Path, roster_path: Path, fake_client):
    config = load_configuration(config_path)
    roster = load_roster(roster_path, config)
    group = build_group_resources(config, roster)[0]
    team = fake_client.add_team(group.team_name, team_id=42)
    repository = fake_client.add_repository(group.repositories[0].name)
    fake_client.permissions[(team.slug, repository.name)] = "push"
    return config, group, team, repository


def _direct_group(student: Student) -> DesiredGroup:
    return DesiredGroup(
        key="group:G01",
        group_id="G01",
        team_name="COMP3018-2026S2-G01",
        repositories=[
            DesiredRepository(
                name="COMP3018-2026S2-G01",
                description="Project",
            )
        ],
        students=[student],
    )


def test_fake_lists_numeric_team_ids_for_pending_invitation(fake_client) -> None:
    invitation = fake_client.add_pending(
        "student@example.edu.au",
        invitation_id=77,
        team_ids=[42, 43],
    )

    team_ids = fake_client.list_invitation_team_ids("teaching-org", invitation.id)

    assert team_ids == {42, 43}
    assert fake_client.calls[-1].operation == "list_invitation_team_ids"
    assert fake_client.calls[-1].method == "GET"


def test_apply_adopts_pre_existing_expected_team_pending_invitation_then_never_resends(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, group, team, _repository = _ready_group(
        config_path,
        roster_path,
        fake_client,
    )
    student = group.students[0]
    invitation = fake_client.add_pending(
        student.email,
        invitation_id=77,
        team_ids=[team.id],
    )
    ledger_file = ledger_path(config_path, config)
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(fake_client, config, [group], ledger)
    plan = build_provision_plan(
        config,
        [group],
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )

    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )

    assert outcome.exit_code == EXIT_SUCCESS
    applied_action = next(action for action in outcome.plan.actions if action.student_id)
    assert applied_action.status == ActionStatus.SUCCEEDED
    persisted = load_ledger(ledger_file, config.organisation)
    assert len(persisted.records) == 1
    assert persisted.records[0].invitation_id == invitation.id
    assert persisted.records[0].team_id == team.id
    assert persisted.records[0].status == InvitationState.PENDING
    assert not fake_client.write_calls

    fake_client.pending.clear()
    fake_client.clear_calls()
    disappeared = discover_snapshot(fake_client, config, [group], persisted)
    repeated_plan = build_provision_plan(
        config,
        [group],
        disappeared,
        mode="Apply",
        generated_at=fixed_now,
    )
    invitation_action = next(action for action in repeated_plan.actions if action.student_id)

    assert invitation_action.action_type == ActionType.REVIEW_REQUIRED
    assert invitation_action.invitation_state == InvitationState.UNRESOLVED
    execute_plan(
        repeated_plan,
        client=fake_client,
        config=config,
        ledger=persisted,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )
    assert not fake_client.write_calls


def test_wrong_team_pending_invitation_is_recorded_failed_then_requires_review(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, group, expected_team, _repository = _ready_group(
        config_path,
        roster_path,
        fake_client,
    )
    wrong_team = fake_client.add_team("COMP3018-2026S2-G99", team_id=99)
    student = group.students[0]
    invitation = fake_client.add_pending(
        student.email,
        invitation_id=78,
        team_ids=[wrong_team.id],
    )
    ledger_file = ledger_path(config_path, config)
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(fake_client, config, [group], ledger)
    plan = build_provision_plan(
        config,
        [group],
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )
    invitation_action = next(action for action in plan.actions if action.student_id)

    assert invitation_action.action_type == ActionType.REVIEW_REQUIRED
    assert "expected team is not attached" in invitation_action.reason
    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )

    assert outcome.exit_code == EXIT_SUCCESS
    applied_action = next(action for action in outcome.plan.actions if action.student_id)
    assert applied_action.status == ActionStatus.REVIEW
    persisted = load_ledger(ledger_file, config.organisation)
    assert persisted.records[0].invitation_id == invitation.id
    assert persisted.records[0].team_id == expected_team.id
    assert persisted.records[0].status == InvitationState.FAILED
    assert "not confirmed as attached" in (persisted.records[0].failure_reason or "")
    assert not fake_client.write_calls

    fake_client.pending.clear()
    disappeared = discover_snapshot(fake_client, config, [group], persisted)
    repeated = build_provision_plan(
        config,
        [group],
        disappeared,
        mode="Apply",
        generated_at=fixed_now,
    )
    repeated_action = next(action for action in repeated.actions if action.student_id)
    assert repeated_action.action_type == ActionType.REVIEW_REQUIRED
    assert repeated_action.invitation_state == InvitationState.FAILED


def test_public_existing_repository_blocks_access_and_invitations(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config = load_configuration(config_path)
    roster = load_roster(roster_path, config)
    group = build_group_resources(config, roster)[0]
    team = fake_client.add_team(group.team_name, team_id=42)
    repository = fake_client.add_repository(
        group.repositories[0].name,
        private=False,
    )
    fake_client.permissions[(team.slug, repository.name)] = "push"
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(fake_client, config, [group], ledger)
    plan = build_provision_plan(
        config,
        [group],
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )

    repository_action = next(
        action
        for action in plan.actions
        if action.repository == repository.name
        and action.action_id.endswith(f"repository:{repository.name.casefold()}")
    )
    assert repository_action.action_type == ActionType.ERROR
    assert "public" in repository_action.reason

    fake_client.clear_calls()
    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_path(config_path, config),
        now=lambda: fixed_now,
    )

    assert outcome.exit_code == EXIT_PARTIAL
    invitation_action = next(action for action in outcome.plan.actions if action.student_id)
    assert invitation_action.status == ActionStatus.BLOCKED
    assert not fake_client.write_calls


def test_controlled_invitation_rejection_is_persisted_and_not_retried(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, group, _team, _repository = _ready_group(
        config_path,
        roster_path,
        fake_client,
    )
    student = group.students[0]
    ledger_file = ledger_path(config_path, config)
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(fake_client, config, [group], ledger)
    plan = build_provision_plan(
        config,
        [group],
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )
    fake_client.fail_next(
        "invite_member",
        GitHubError("GitHub rejected the invitation"),
        target=student.email.casefold(),
    )

    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )

    assert outcome.exit_code == EXIT_PARTIAL
    persisted = load_ledger(ledger_file, config.organisation)
    assert persisted.records[0].status == InvitationState.FAILED

    fake_client.clear_calls()
    repeated_snapshot = discover_snapshot(
        fake_client,
        config,
        [group],
        persisted,
    )
    repeated_plan = build_provision_plan(
        config,
        [group],
        repeated_snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )
    repeated_action = next(action for action in repeated_plan.actions if action.student_id)
    assert repeated_action.action_type == ActionType.REVIEW_REQUIRED
    execute_plan(
        repeated_plan,
        client=fake_client,
        config=config,
        ledger=persisted,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )
    assert not fake_client.write_calls


def test_live_pending_invitation_heals_stale_expired_ledger_record(
    config_factory,
    roster_factory,
    fake_client,
    record_factory,
    fixed_now,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, group, team, _repository = _ready_group(
        config_path,
        roster_path,
        fake_client,
    )
    student = group.students[0]
    fake_client.add_pending(
        student.email,
        invitation_id=3200,
        team_ids=[team.id],
    )
    ledger_file = ledger_path(config_path, config)
    ledger = InvitationLedger(
        organisation=config.organisation,
        records=[
            record_factory(
                student_id=student.student_id,
                email=student.email,
                group_id=group.group_id,
                team_name=group.team_name,
                team_id=team.id,
                invitation_id=3100,
                status=InvitationState.EXPIRED,
                attempt_count=2,
            )
        ],
    )
    snapshot = discover_snapshot(fake_client, config, [group], ledger)
    plan = build_provision_plan(
        config,
        [group],
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )

    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )

    assert outcome.exit_code == EXIT_SUCCESS
    healed = load_ledger(ledger_file, config.organisation)
    assert healed.records[0].status == InvitationState.PENDING
    assert healed.records[0].invitation_id == 3200
    assert healed.records[0].attempt_count == 3
    assert not fake_client.write_calls

    fake_client.pending.clear()
    repeated_snapshot = discover_snapshot(
        fake_client,
        config,
        [group],
        healed,
        require_template=False,
    )
    repeated_plan = build_provision_plan(
        config,
        [group],
        repeated_snapshot,
        mode="Apply",
        generated_at=fixed_now,
        retry_expired=True,
        provision_resources=False,
    )
    repeated_action = next(action for action in repeated_plan.actions if action.student_id)
    assert repeated_action.action_type == ActionType.REVIEW_REQUIRED
    assert repeated_action.invitation_state == InvitationState.UNRESOLVED


def test_pending_invitation_ledger_failure_is_an_unexpected_failure(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
    monkeypatch,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, group, team, _repository = _ready_group(
        config_path,
        roster_path,
        fake_client,
    )
    fake_client.add_pending(
        group.students[0].email,
        invitation_id=78,
        team_ids=[team.id],
    )
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(fake_client, config, [group], ledger)
    plan = build_provision_plan(
        config,
        [group],
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )

    def fail_save(_path, _ledger) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(core, "save_ledger_atomic", fail_save)
    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_path(config_path, config),
        now=lambda: fixed_now,
    )

    assert outcome.exit_code == EXIT_UNEXPECTED
    invitation_action = next(action for action in outcome.plan.actions if action.student_id)
    assert invitation_action.status == ActionStatus.FAILED
    assert "disk full" in (invitation_action.error or "")
    assert not fake_client.write_calls


@pytest.mark.parametrize("attached_to_expected_team", [True, False])
def test_transport_error_recovery_only_adopts_expected_team_invitation(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
    attached_to_expected_team,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, group, expected_team, _repository = _ready_group(
        config_path,
        roster_path,
        fake_client,
    )
    student = group.students[0]
    ledger_file = ledger_path(config_path, config)
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(fake_client, config, [group], ledger)
    plan = build_provision_plan(
        config,
        [group],
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )
    attached_team = expected_team
    if not attached_to_expected_team:
        attached_team = fake_client.add_team("COMP3018-2026S2-G99", team_id=99)
    pending = fake_client.add_pending(
        student.email,
        invitation_id=79,
        team_ids=[attached_team.id],
    )
    fake_client.fail_next(
        "invite_member",
        GitHubNetworkError("connection timed out"),
        target=student.email.casefold(),
    )

    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )
    invitation_action = next(action for action in outcome.plan.actions if action.student_id)
    persisted = load_ledger(ledger_file, config.organisation)

    if attached_to_expected_team:
        assert outcome.exit_code == EXIT_SUCCESS
        assert invitation_action.status == ActionStatus.SUCCEEDED
        assert persisted.records[0].invitation_id == pending.id
        assert persisted.records[0].status == InvitationState.PENDING
    else:
        assert outcome.exit_code == EXIT_PARTIAL
        assert invitation_action.status == ActionStatus.FAILED
        assert persisted.records[0].invitation_id is None
        assert persisted.records[0].status == InvitationState.FAILED


def test_duplicate_github_login_is_case_insensitively_rejected(
    config_factory,
    roster_factory,
) -> None:
    config = load_configuration(
        config_factory(overrides={"roster": {"github_login_column": "github_login"}})
    )
    roster_path = roster_factory(
        [
            {
                "student_id": "10000001",
                "email": "one@student.example.edu.au",
                "group_id": "G01",
                "github_login": "Known-Login",
            },
            {
                "student_id": "10000002",
                "email": "two@student.example.edu.au",
                "group_id": "G02",
                "github_login": "known-login",
            },
        ],
        headers=["student_id", "email", "group_id", "github_login"],
    )

    with pytest.raises(InputValidationError, match="duplicate github_login"):
        load_roster(roster_path, config)


@pytest.mark.parametrize("reserved_column", ["student_id", "email", "group_id"])
def test_github_login_column_rejects_reserved_roster_headers(
    config_factory,
    reserved_column,
) -> None:
    config_path = config_factory(overrides={"roster": {"github_login_column": reserved_column}})

    with pytest.raises(InputValidationError, match="github_login_column"):
        load_configuration(config_path)


def test_custom_github_permission_string_plans_an_update(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, group, team, repository = _ready_group(
        config_path,
        roster_path,
        fake_client,
    )
    fake_client.permissions[(team.slug, repository.name)] = "custom-course-role"
    snapshot = discover_snapshot(
        fake_client,
        config,
        [group],
        InvitationLedger(organisation=config.organisation),
    )

    plan = build_provision_plan(
        config,
        [group],
        snapshot,
        mode="Dry run",
        generated_at=fixed_now,
    )
    permission_action = next(
        action for action in plan.actions if action.action_id.startswith("group:G01:permission:")
    )

    assert permission_action.action_type == ActionType.UPDATE_TEAM_REPOSITORY_PERMISSION
    assert permission_action.current_state == "custom-course-role"
    assert permission_action.desired_state == "push"


def test_exact_expired_failure_evidence_outranks_shared_team_inference(
    record_factory,
) -> None:
    student = Student(
        student_id="12345678",
        email="student@example.edu.au",
        group_id="G01",
    )
    group = _direct_group(student)
    team = Team(id=42, name=group.team_name, slug="expected-team")
    record = record_factory(
        student_id=student.student_id,
        email=student.email,
        group_id=group.group_id,
        team_name=group.team_name,
        team_id=team.id,
        invitation_id=700,
        status=InvitationState.PENDING,
    )
    snapshot = Snapshot(
        teams=[team],
        repositories=[],
        pending_invitations=[],
        failed_invitations=[
            FailedInvitation(
                id=700,
                email=student.email,
                failed_reason="Invitation expired",
            )
        ],
        team_members={
            team.slug: [
                TeamMember(
                    id=1,
                    login="unmapped-member",
                    role="member",
                    inherited=False,
                )
            ]
        },
        permissions={},
        ledger=InvitationLedger(
            organisation="teaching-org",
            records=[record],
        ),
    )

    decision = reconcile_invitation(student, group, snapshot)

    assert decision.state == InvitationState.EXPIRED
    assert decision.action_type == ActionType.REVIEW_REQUIRED


def test_stale_failed_email_without_ledger_is_not_retryable(
    config_factory,
    fixed_now,
) -> None:
    config = load_configuration(config_factory())
    student = Student(
        student_id="12345678",
        email="student@example.edu.au",
        group_id="G01",
    )
    group = _direct_group(student)
    team = Team(id=42, name=group.team_name, slug="expected-team")
    repository = Repository(
        name=group.repositories[0].name,
        is_private=True,
    )
    snapshot = Snapshot(
        teams=[team],
        repositories=[repository],
        pending_invitations=[],
        failed_invitations=[
            FailedInvitation(
                id=700,
                email=student.email,
                failed_reason="Invitation expired",
            )
        ],
        team_members={team.slug: []},
        permissions={f"{team.slug}\0{repository.name.casefold()}": "push"},
        ledger=InvitationLedger(organisation=config.organisation),
    )

    decision = reconcile_invitation(student, group, snapshot)
    retry_plan = build_provision_plan(
        config,
        [group],
        snapshot,
        mode="Dry run",
        generated_at=fixed_now,
        retry_expired=True,
        provision_resources=False,
    )
    retry_action = next(action for action in retry_plan.actions if action.student_id)

    assert decision.state == InvitationState.NOT_INVITED
    assert decision.action_type == ActionType.SEND_INVITATION
    assert retry_action.action_type == ActionType.SKIP_UNCHANGED
    assert retry_action.status == ActionStatus.SKIPPED


def test_changed_email_for_same_student_is_unresolved(
    record_factory,
) -> None:
    student = Student(
        student_id="12345678",
        email="new-address@example.edu.au",
        group_id="G01",
    )
    group = _direct_group(student)
    team = Team(id=42, name=group.team_name, slug="expected-team")
    old_record = record_factory(
        student_id=student.student_id,
        email="old-address@example.edu.au",
        group_id=group.group_id,
        team_name=group.team_name,
        team_id=team.id,
        status=InvitationState.PENDING,
    )
    snapshot = Snapshot(
        teams=[team],
        repositories=[Repository(name=group.repositories[0].name)],
        pending_invitations=[],
        failed_invitations=[],
        team_members={team.slug: []},
        permissions={},
        ledger=InvitationLedger(
            organisation="teaching-org",
            records=[old_record],
        ),
    )

    decision = reconcile_invitation(student, group, snapshot)

    assert decision.state == InvitationState.UNRESOLVED
    assert decision.action_type == ActionType.REVIEW_REQUIRED
    assert "different email" in decision.reason
