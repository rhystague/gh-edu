from __future__ import annotations

from datetime import timedelta

import pytest

from gh_edu.core import (
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    ActionStatus,
    ActionType,
    InvitationLedger,
    InvitationState,
    build_group_resources,
    build_individual_resource,
    build_provision_plan,
    build_semester_close_plan,
    discover_snapshot,
    execute_plan,
    ledger_path,
    load_configuration,
    load_ledger,
    load_roster,
    render_plan_report,
    save_ledger_atomic,
    verify_execution,
    write_plan_report,
)
from gh_edu.github import FailedInvitation, GitHubError


def _load_group_inputs(config_path, roster_path):
    config = load_configuration(config_path)
    roster = load_roster(roster_path, config)
    return config, build_group_resources(config, roster)


def test_group_lifecycle_dry_run_apply_and_repeat_is_idempotent(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
    operation_names,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, groups = _load_group_inputs(config_path, roster_path)
    ledger_file = ledger_path(config_path, config)
    ledger = load_ledger(ledger_file, config.organisation)
    snapshot = discover_snapshot(fake_client, config, groups, ledger)

    dry_plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Dry run",
        generated_at=fixed_now,
    )
    dry_report = write_plan_report(
        config_path,
        config,
        dry_plan,
        kind="provision-plan",
    )

    assert not fake_client.write_calls
    assert not ledger_file.exists()
    assert dry_report.suffix == ".md"
    dry_markdown = dry_report.read_text(encoding="utf-8")
    assert dry_markdown.startswith("# GitHub Provisioning Plan\n")
    assert "- Mode: `Dry run`" in dry_markdown
    assert "| Create teams | 1 |" in dry_markdown
    assert "| Create repositories | 1 |" in dry_markdown
    assert "| Send invitations | 1 |" in dry_markdown

    apply_plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )
    outcome = execute_plan(
        apply_plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )
    outcome = verify_execution(outcome, client=fake_client, config=config)

    assert outcome.exit_code == EXIT_SUCCESS
    assert outcome.successful_writes == 4
    assert operation_names(fake_client) == [
        "create_team",
        "create_repository_from_template",
        "set_team_repository_permission",
        "invite_member",
    ]
    invitation_call = next(
        call for call in fake_client.write_calls if call.operation == "invite_member"
    )
    created_team = fake_client.teams["COMP3018-2026S2-G01"]
    assert invitation_call.payload["team_ids"] == (created_team.id,)
    assert "12345678" not in str(
        {key: value for key, value in invitation_call.payload.items() if key != "email"}
    )
    persisted = load_ledger(ledger_file, config.organisation)
    assert len(persisted.records) == 1
    assert persisted.records[0].status == InvitationState.PENDING
    assert persisted.records[0].attempt_count == 1
    first_invitation_id = persisted.records[0].invitation_id

    fake_client.clear_calls()
    repeated_snapshot = discover_snapshot(
        fake_client,
        config,
        groups,
        load_ledger(ledger_file, config.organisation),
    )
    repeated_plan = build_provision_plan(
        config,
        groups,
        repeated_snapshot,
        mode="Apply",
        generated_at=fixed_now + timedelta(seconds=1),
    )
    repeated_outcome = execute_plan(
        repeated_plan,
        client=fake_client,
        config=config,
        ledger=repeated_snapshot.ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now + timedelta(seconds=1),
    )

    assert repeated_outcome.exit_code == EXIT_SUCCESS
    assert not fake_client.write_calls
    repeated_ledger = load_ledger(ledger_file, config.organisation)
    assert len(repeated_ledger.records) == 1
    assert repeated_ledger.records[0].invitation_id == first_invitation_id
    assert repeated_ledger.records[0].attempt_count == 1
    assert repeated_ledger.records[0].status == InvitationState.PENDING
    invitation_action = next(
        action for action in repeated_outcome.plan.actions if action.student_id
    )
    assert invitation_action.action_type == ActionType.SKIP_PENDING_INVITATION
    assert invitation_action.status == ActionStatus.SKIPPED


@pytest.mark.parametrize(
    ("state", "expected_type"),
    [
        ("pending", ActionType.SKIP_PENDING_INVITATION),
        ("unresolved", ActionType.REVIEW_REQUIRED),
        ("inferred", ActionType.SKIP_ACCEPTED),
    ],
)
def test_pending_unresolved_and_inferred_students_are_never_resent(
    config_factory,
    roster_factory,
    fake_client,
    record_factory,
    fixed_now,
    state,
    expected_type,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, groups = _load_group_inputs(config_path, roster_path)
    group = groups[0]
    student = group.students[0]
    team = fake_client.add_team(group.team_name, team_id=42)
    repository = fake_client.add_repository(group.repositories[0].name)
    fake_client.permissions[(team.slug, repository.name)] = "push"
    records = []
    if state == "pending":
        fake_client.add_pending(student.email.swapcase(), team_ids=[team.id])
    else:
        records.append(
            record_factory(
                email=student.email,
                group_id=group.group_id,
                team_name=group.team_name,
                team_id=team.id,
                status=InvitationState.PENDING,
            )
        )
        if state == "inferred":
            fake_client.add_member(team.slug, "unmapped-student")
    ledger = InvitationLedger(organisation=config.organisation, records=records)
    snapshot = discover_snapshot(fake_client, config, groups, ledger)
    plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )

    invitation_action = next(action for action in plan.actions if action.student_id)
    assert invitation_action.action_type == expected_type

    fake_client.clear_calls()
    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_path(config_path, config),
        now=lambda: fixed_now,
    )

    assert outcome.exit_code == EXIT_SUCCESS
    assert not [call for call in fake_client.write_calls if call.operation == "invite_member"]


def test_individual_workflow_uses_numeric_team_id_and_confirms_acceptance(
    config_factory,
    fake_client,
    fixed_now,
) -> None:
    config_path = config_factory()
    config = load_configuration(config_path)
    group = build_individual_resource(
        config,
        student_id="12345678",
        email="12345678@student.example.edu.au",
        repository="CAPSTONE-SPECIAL-12345678",
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
        title="GitHub Individual Provisioning Plan",
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
    team = fake_client.teams["IND-12345678"]
    invitation_call = next(
        call for call in fake_client.write_calls if call.operation == "invite_member"
    )
    assert invitation_call.payload["team_ids"] == (team.id,)
    assert "CAPSTONE-SPECIAL-12345678" in fake_client.repositories

    fake_client.clear_calls()
    pending_snapshot = discover_snapshot(
        fake_client,
        config,
        [group],
        load_ledger(ledger_file, config.organisation),
    )
    pending_plan = build_provision_plan(
        config,
        [group],
        pending_snapshot,
        mode="Apply",
        generated_at=fixed_now + timedelta(seconds=1),
    )
    pending_outcome = execute_plan(
        pending_plan,
        client=fake_client,
        config=config,
        ledger=pending_snapshot.ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now + timedelta(seconds=1),
    )
    assert pending_outcome.exit_code == EXIT_SUCCESS
    assert not fake_client.write_calls

    fake_client.accept_invitation(
        group.students[0].email,
        login="new-student-login",
        team_slugs=[team.slug],
    )
    accepted_snapshot = discover_snapshot(
        fake_client,
        config,
        [group],
        load_ledger(ledger_file, config.organisation),
    )
    accepted_plan = build_provision_plan(
        config,
        [group],
        accepted_snapshot,
        mode="Apply",
        generated_at=fixed_now + timedelta(seconds=2),
    )
    accepted_action = next(action for action in accepted_plan.actions if action.student_id)
    assert accepted_action.invitation_state == InvitationState.ACCEPTED_CONFIRMED
    assert accepted_action.action_type == ActionType.SKIP_ACCEPTED


def test_explicit_expired_retry_updates_ledger_once_and_then_skips_pending(
    config_factory,
    roster_factory,
    fake_client,
    record_factory,
    fixed_now,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, groups = _load_group_inputs(config_path, roster_path)
    group = groups[0]
    student = group.students[0]
    team = fake_client.add_team(group.team_name, team_id=42)
    repository = fake_client.add_repository(group.repositories[0].name)
    fake_client.permissions[(team.slug, repository.name)] = "push"
    ledger_file = ledger_path(config_path, config)
    ledger = InvitationLedger(
        organisation=config.organisation,
        records=[
            record_factory(
                email=student.email,
                group_id=group.group_id,
                team_name=group.team_name,
                team_id=team.id,
                invitation_id=2999,
                status=InvitationState.EXPIRED,
                attempt_count=1,
            )
        ],
    )
    save_ledger_atomic(ledger_file, ledger)
    snapshot = discover_snapshot(
        fake_client,
        config,
        groups,
        ledger,
        require_template=False,
    )
    ordinary_plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )
    ordinary_invitation = next(action for action in ordinary_plan.actions if action.student_id)
    assert ordinary_invitation.action_type == ActionType.REVIEW_REQUIRED

    retry_plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
        retry_expired=True,
        provision_resources=False,
    )
    retry_outcome = execute_plan(
        retry_plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )

    assert retry_outcome.exit_code == EXIT_SUCCESS
    invitation_writes = [
        call for call in fake_client.write_calls if call.operation == "invite_member"
    ]
    assert len(invitation_writes) == 1
    updated = load_ledger(ledger_file, config.organisation)
    assert updated.records[0].status == InvitationState.PENDING
    assert updated.records[0].attempt_count == 2
    assert updated.records[0].invitation_id == 3000

    fake_client.clear_calls()
    repeated_snapshot = discover_snapshot(
        fake_client,
        config,
        groups,
        updated,
        require_template=False,
    )
    repeated_plan = build_provision_plan(
        config,
        groups,
        repeated_snapshot,
        mode="Apply",
        generated_at=fixed_now + timedelta(seconds=1),
        retry_expired=True,
        provision_resources=False,
    )
    repeated_outcome = execute_plan(
        repeated_plan,
        client=fake_client,
        config=config,
        ledger=updated,
        ledger_file=ledger_file,
        now=lambda: fixed_now + timedelta(seconds=1),
    )

    assert repeated_outcome.exit_code == EXIT_SUCCESS
    assert not fake_client.write_calls


def test_failed_expired_retry_is_not_retried_again_from_stale_expiry_evidence(
    config_factory,
    roster_factory,
    fake_client,
    record_factory,
    fixed_now,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, groups = _load_group_inputs(config_path, roster_path)
    group = groups[0]
    student = group.students[0]
    team = fake_client.add_team(group.team_name, team_id=42)
    repository = fake_client.add_repository(group.repositories[0].name)
    fake_client.permissions[(team.slug, repository.name)] = "push"
    fake_client.failed_invitations.append(
        FailedInvitation(
            id=2999,
            email=student.email,
            failed_reason="Invitation expired",
        )
    )
    ledger_file = ledger_path(config_path, config)
    ledger = InvitationLedger(
        organisation=config.organisation,
        records=[
            record_factory(
                email=student.email,
                group_id=group.group_id,
                team_name=group.team_name,
                team_id=team.id,
                invitation_id=2999,
                status=InvitationState.EXPIRED,
            )
        ],
    )
    snapshot = discover_snapshot(
        fake_client,
        config,
        groups,
        ledger,
        require_template=False,
    )
    retry_plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
        retry_expired=True,
        provision_resources=False,
    )
    fake_client.fail_next(
        "invite_member",
        GitHubError("GitHub rejected the retry"),
        target=student.email.casefold(),
    )

    outcome = execute_plan(
        retry_plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )

    assert outcome.exit_code == EXIT_PARTIAL
    failed_ledger = load_ledger(ledger_file, config.organisation)
    assert failed_ledger.records[0].status == InvitationState.FAILED

    fake_client.clear_calls()
    repeated_snapshot = discover_snapshot(
        fake_client,
        config,
        groups,
        failed_ledger,
        require_template=False,
    )
    repeated_plan = build_provision_plan(
        config,
        groups,
        repeated_snapshot,
        mode="Apply",
        generated_at=fixed_now + timedelta(seconds=1),
        retry_expired=True,
        provision_resources=False,
    )
    repeated_action = next(action for action in repeated_plan.actions if action.student_id)
    assert repeated_action.action_type == ActionType.REVIEW_REQUIRED
    assert repeated_action.invitation_state == InvitationState.FAILED
    execute_plan(
        repeated_plan,
        client=fake_client,
        config=config,
        ledger=failed_ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now + timedelta(seconds=1),
    )
    assert not fake_client.write_calls


def test_resource_failure_blocks_dependants_but_unrelated_group_continues(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory(
        [
            {
                "student_id": "10000001",
                "email": "one@student.example.edu.au",
                "group_id": "G01",
            },
            {
                "student_id": "10000002",
                "email": "two@student.example.edu.au",
                "group_id": "G02",
            },
        ]
    )
    config, groups = _load_group_inputs(config_path, roster_path)
    ledger = InvitationLedger(organisation=config.organisation)
    ledger_file = ledger_path(config_path, config)
    snapshot = discover_snapshot(fake_client, config, groups, ledger)
    fake_client.fail_next(
        "create_repository_from_template",
        GitHubError("simulated repository failure"),
        target="COMP3018-2026S2-G01",
    )
    plan = build_provision_plan(
        config,
        groups,
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

    assert outcome.exit_code == EXIT_PARTIAL
    g01_permission = next(
        action
        for action in outcome.plan.actions
        if action.scope == "group:G01" and action.action_type == ActionType.GRANT_TEAM_REPOSITORY
    )
    g01_invitation = next(
        action
        for action in outcome.plan.actions
        if action.scope == "group:G01" and action.student_id
    )
    assert g01_permission.status == ActionStatus.BLOCKED
    assert g01_invitation.status == ActionStatus.BLOCKED
    assert "COMP3018-2026S2-G02" in fake_client.repositories
    assert "two@student.example.edu.au" in {
        invitation.email for invitation in fake_client.pending.values()
    }
    persisted = load_ledger(ledger_file, config.organisation)
    assert [record.student_id for record in persisted.records] == ["10000002"]
    markdown = render_plan_report(outcome.plan)
    assert "## Errors and blocked changes" in markdown
    assert "simulated repository failure" in markdown


def test_semester_close_removes_access_before_archiving_and_repeat_is_noop(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
    operation_names,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, groups = _load_group_inputs(config_path, roster_path)
    group = groups[0]
    team = fake_client.add_team(group.team_name, team_id=42)
    repository = fake_client.add_repository(group.repositories[0].name)
    fake_client.permissions[(team.slug, repository.name)] = "push"
    ledger_file = ledger_path(config_path, config)
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(
        fake_client,
        config,
        groups,
        ledger,
        require_template=False,
    )
    close_plan = build_semester_close_plan(
        config,
        groups,
        snapshot,
        archive_repositories=True,
        remove_team_access=True,
        mode="Apply",
        generated_at=fixed_now,
    )
    close_writes = [action for action in close_plan.actions if action.is_write]
    assert [action.action_type for action in close_writes] == [
        ActionType.REMOVE_TEAM_REPOSITORY,
        ActionType.ARCHIVE_REPOSITORY,
    ]
    assert close_writes[1].dependencies == [close_writes[0].action_id]

    outcome = execute_plan(
        close_plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )
    outcome = verify_execution(outcome, client=fake_client, config=config)

    assert outcome.exit_code == EXIT_SUCCESS
    assert operation_names(fake_client) == [
        "remove_team_repository",
        "archive_repository",
    ]
    assert (team.slug, repository.name) not in fake_client.permissions
    assert fake_client.repositories[repository.name].is_archived

    fake_client.clear_calls()
    repeated_snapshot = discover_snapshot(
        fake_client,
        config,
        groups,
        ledger,
        require_template=False,
    )
    repeated_plan = build_semester_close_plan(
        config,
        groups,
        repeated_snapshot,
        archive_repositories=True,
        remove_team_access=True,
        mode="Apply",
        generated_at=fixed_now + timedelta(seconds=1),
    )
    repeated_outcome = execute_plan(
        repeated_plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now + timedelta(seconds=1),
    )

    assert repeated_outcome.exit_code == EXIT_SUCCESS
    assert not fake_client.write_calls
    assert "delete_team" not in operation_names(fake_client)
    assert "delete_repository" not in operation_names(fake_client)


def test_unexpected_post_apply_verification_failure_is_reportable(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config, groups = _load_group_inputs(config_path, roster_path)
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(fake_client, config, groups, ledger)
    plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )
    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_path(config_path, config),
        now=lambda: fixed_now,
    )
    fake_client.fail_next(
        "list_teams",
        RuntimeError("unexpected verification bug"),
    )

    verified = verify_execution(
        outcome,
        client=fake_client,
        config=config,
    )

    assert verified.exit_code == EXIT_PARTIAL
    verification_error = next(
        action for action in verified.plan.actions if action.action_id == "verify:unexpected"
    )
    assert verification_error.status == ActionStatus.FAILED
    assert "unexpected verification bug" in verification_error.reason
