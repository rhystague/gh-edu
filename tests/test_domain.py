from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gh_edu.core import (
    ActionStatus,
    ActionType,
    DesiredGroup,
    DesiredRepository,
    InputValidationError,
    InvitationLedger,
    InvitationState,
    Permission,
    Snapshot,
    Student,
    build_group_resources,
    build_provision_plan,
    discover_snapshot,
    load_configuration,
    load_roster,
    permission_key,
    reconcile_invitation,
)
from gh_edu.github import FailedInvitation, Invitation, Repository, Team


def test_loads_valid_roster_and_preserves_string_student_id(
    config_factory,
    roster_factory,
) -> None:
    config = load_configuration(config_factory())
    roster = load_roster(
        roster_factory(
            [
                {
                    "student_id": "00123456",
                    "email": "00123456@student.example.edu.au",
                    "group_id": "G01",
                }
            ]
        ),
        config,
    )

    assert roster.students[0].student_id == "00123456"
    assert build_group_resources(config, roster)[0].team_name == "COMP3018-2026S2-G01"


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                {
                    "student_id": "12345678",
                    "email": "not-an-email",
                    "group_id": "G01",
                }
            ],
            "email is malformed",
        ),
        (
            [
                {
                    "student_id": "12345678",
                    "email": "one@student.example.edu.au",
                    "group_id": "G01",
                },
                {
                    "student_id": "12345678",
                    "email": "two@student.example.edu.au",
                    "group_id": "G02",
                },
            ],
            "duplicate student_id",
        ),
        (
            [
                {
                    "student_id": "12345678",
                    "email": "Same@Student.Example.Edu.Au",
                    "group_id": "G01",
                },
                {
                    "student_id": "87654321",
                    "email": "same@student.example.edu.au",
                    "group_id": "G02",
                },
            ],
            "duplicate email",
        ),
    ],
    ids=["malformed-email", "duplicate-id", "case-insensitive-duplicate-email"],
)
def test_roster_validation_rejects_invalid_identities(
    config_factory,
    roster_factory,
    rows,
    message,
) -> None:
    config = load_configuration(config_factory())

    with pytest.raises(InputValidationError, match=message):
        load_roster(roster_factory(rows), config)


def test_roster_requires_all_structural_columns(
    config_factory,
    roster_factory,
) -> None:
    config = load_configuration(config_factory())
    roster_path = roster_factory(
        [{"student_id": "12345678", "email": "student@example.edu.au"}],
        headers=["student_id", "email"],
    )

    with pytest.raises(InputValidationError, match="group_id"):
        load_roster(roster_path, config)


def test_generated_names_reject_normalised_collisions(
    config_factory,
    roster_factory,
) -> None:
    config = load_configuration(config_factory())
    roster = load_roster(
        roster_factory(
            [
                {
                    "student_id": "10000001",
                    "email": "one@student.example.edu.au",
                    "group_id": "G-01",
                },
                {
                    "student_id": "10000002",
                    "email": "two@student.example.edu.au",
                    "group_id": "G_01",
                },
            ]
        ),
        config,
    )

    with pytest.raises(InputValidationError, match="normalise to the same name"):
        build_group_resources(config, roster)


def test_discovery_rejects_existing_normalised_collision_before_writes(
    config_factory,
    roster_factory,
    fake_client,
) -> None:
    config = load_configuration(config_factory())
    roster = load_roster(roster_factory(), config)
    groups = build_group_resources(config, roster)
    fake_client.add_team("COMP3018_2026S2_G01")

    with pytest.raises(InputValidationError, match="collides with existing team"):
        discover_snapshot(
            fake_client,
            config,
            groups,
            InvitationLedger(organisation=config.organisation),
        )

    assert not fake_client.write_calls


def _student(*, github_login: str | None = None) -> Student:
    return Student(
        student_id="12345678",
        email="Student@Example.edu.au",
        group_id="G01",
        github_login=github_login,
    )


def _group(student: Student, *, individual: bool = False) -> DesiredGroup:
    return DesiredGroup(
        key=("individual:12345678" if individual else "group:G01"),
        group_id=("IND-12345678" if individual else "G01"),
        team_name=("IND-12345678" if individual else "COMP3018-2026S2-G01"),
        repositories=[DesiredRepository(name="COMP3018-2026S2-G01", description="Project")],
        students=[student],
        individual=individual,
    )


def _snapshot(
    *,
    student: Student,
    individual: bool = False,
    pending: bool = False,
    members: set[str] | None = None,
    records=(),
    failed=(),
) -> tuple[DesiredGroup, Snapshot]:
    group = _group(student, individual=individual)
    team = Team(id=42, name=group.team_name, slug="expected-team")
    invitations = (
        [
            Invitation(
                id=99,
                email=student.email.swapcase(),
                team_ids=(team.id,),
            )
        ]
        if pending
        else []
    )
    return group, Snapshot(
        teams=[team],
        repositories=[],
        pending_invitations=invitations,
        failed_invitations=list(failed),
        team_members={team.slug: set(members or set())},
        permissions={},
        ledger=InvitationLedger(organisation="teaching-org", records=list(records)),
    )


@pytest.mark.parametrize(
    (
        "case",
        "pending",
        "individual",
        "github_login",
        "members",
        "ledger_status",
        "failed_reason",
        "expected_state",
        "expected_action",
    ),
    [
        (
            "pending-wins",
            True,
            False,
            "known-login",
            {"known-login"},
            InvitationState.EXPIRED,
            None,
            InvitationState.PENDING,
            ActionType.SKIP_PENDING_INVITATION,
        ),
        (
            "new",
            False,
            False,
            None,
            set(),
            None,
            None,
            InvitationState.NOT_INVITED,
            ActionType.SEND_INVITATION,
        ),
        (
            "known-login",
            False,
            False,
            "Known-Login",
            {"known-login"},
            InvitationState.PENDING,
            None,
            InvitationState.ACCEPTED_CONFIRMED,
            ActionType.SKIP_ACCEPTED,
        ),
        (
            "individual-one-member",
            False,
            True,
            None,
            {"some-login"},
            InvitationState.PENDING,
            None,
            InvitationState.ACCEPTED_CONFIRMED,
            ActionType.SKIP_ACCEPTED,
        ),
        (
            "individual-ambiguous",
            False,
            True,
            None,
            {"one", "two"},
            InvitationState.PENDING,
            None,
            InvitationState.UNRESOLVED,
            ActionType.REVIEW_REQUIRED,
        ),
        (
            "prior-accepted",
            False,
            False,
            None,
            set(),
            InvitationState.ACCEPTED_INFERRED,
            None,
            InvitationState.ACCEPTED_INFERRED,
            ActionType.SKIP_ACCEPTED,
        ),
        (
            "explicit-expiry-ledger",
            False,
            False,
            None,
            set(),
            InvitationState.EXPIRED,
            None,
            InvitationState.EXPIRED,
            ActionType.REVIEW_REQUIRED,
        ),
        (
            "explicit-expiry-github",
            False,
            False,
            None,
            set(),
            InvitationState.PENDING,
            "Invitation expired",
            InvitationState.EXPIRED,
            ActionType.REVIEW_REQUIRED,
        ),
        (
            "failed",
            False,
            False,
            None,
            set(),
            InvitationState.FAILED,
            None,
            InvitationState.FAILED,
            ActionType.REVIEW_REQUIRED,
        ),
        (
            "shared-inferred",
            False,
            False,
            None,
            {"unmapped-login"},
            InvitationState.PENDING,
            None,
            InvitationState.ACCEPTED_INFERRED,
            ActionType.SKIP_ACCEPTED,
        ),
        (
            "unresolved",
            False,
            False,
            None,
            set(),
            InvitationState.PENDING,
            None,
            InvitationState.UNRESOLVED,
            ActionType.REVIEW_REQUIRED,
        ),
    ],
)
def test_invitation_reconciliation_state_and_precedence(
    record_factory,
    case,
    pending,
    individual,
    github_login,
    members,
    ledger_status,
    failed_reason,
    expected_state,
    expected_action,
) -> None:
    student = _student(github_login=github_login)
    group = _group(student, individual=individual)
    records = (
        [
            record_factory(
                email=student.email,
                group_id=group.group_id,
                team_name=group.team_name,
                team_id=42,
                status=ledger_status,
            )
        ]
        if ledger_status is not None
        else []
    )
    failed = (
        [
            FailedInvitation(
                id=records[0].invitation_id if records else 99,
                email=student.email,
                failed_reason=failed_reason,
            )
        ]
        if failed_reason is not None
        else []
    )
    group, snapshot = _snapshot(
        student=student,
        individual=individual,
        pending=pending,
        members=members,
        records=records,
        failed=failed,
    )

    decision = reconcile_invitation(student, group, snapshot)

    assert decision.state == expected_state, case
    assert decision.action_type == expected_action, case


def test_old_missing_pending_invitation_is_unresolved_not_expired(
    record_factory,
) -> None:
    student = _student()
    record = record_factory(
        email=student.email,
        invited_at=datetime.now(UTC) - timedelta(days=30),
        status=InvitationState.PENDING,
    )
    group, snapshot = _snapshot(student=student, records=[record])

    decision = reconcile_invitation(student, group, snapshot)

    assert decision.state == InvitationState.UNRESOLVED
    assert decision.action_type == ActionType.REVIEW_REQUIRED


def test_same_email_recorded_for_other_team_requires_review(
    record_factory,
) -> None:
    student = _student()
    other_record = record_factory(
        email=student.email,
        group_id="G02",
        team_name="COMP3018-2026S2-G02",
    )
    group, snapshot = _snapshot(student=student, records=[other_record])

    decision = reconcile_invitation(student, group, snapshot)

    assert decision.state == InvitationState.UNRESOLVED
    assert decision.action_type == ActionType.REVIEW_REQUIRED


def test_recreated_team_numeric_id_requires_review(
    record_factory,
) -> None:
    student = _student()
    record = record_factory(
        email=student.email,
        team_id=999,
        status=InvitationState.EXPIRED,
    )
    group, snapshot = _snapshot(student=student, records=[record])

    decision = reconcile_invitation(student, group, snapshot)

    assert decision.state == InvitationState.UNRESOLVED
    assert decision.action_type == ActionType.REVIEW_REQUIRED
    assert "different numeric ID" in decision.reason


def test_multi_repository_plan_has_independent_repository_and_permission_actions(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    config = load_configuration(
        config_factory(
            repositories=[
                {
                    "name": "{subject}-{term}-{group_id}",
                    "description": "Project {group_id}",
                },
                {
                    "name": "{subject}-{term}-{group_id}-documentation",
                    "description": "Documentation {group_id}",
                },
            ]
        )
    )
    roster = load_roster(roster_factory(), config)
    groups = build_group_resources(config, roster)
    snapshot = discover_snapshot(
        fake_client,
        config,
        groups,
        InvitationLedger(organisation=config.organisation),
    )

    plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Dry run",
        generated_at=fixed_now,
    )

    assert [
        action.repository
        for action in plan.actions
        if action.action_type == ActionType.CREATE_REPOSITORY
    ] == [
        "COMP3018-2026S2-G01",
        "COMP3018-2026S2-G01-documentation",
    ]
    permissions = [
        action for action in plan.actions if action.action_type == ActionType.GRANT_TEAM_REPOSITORY
    ]
    assert len(permissions) == 2
    assert all(len(action.dependencies) == 2 for action in permissions)


@pytest.mark.parametrize(
    ("current_permission", "expected_action"),
    [
        (None, ActionType.GRANT_TEAM_REPOSITORY),
        ("pull", ActionType.UPDATE_TEAM_REPOSITORY_PERMISSION),
        ("push", ActionType.SKIP_UNCHANGED),
    ],
)
def test_permission_planning_grants_updates_or_skips(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
    current_permission,
    expected_action,
) -> None:
    config = load_configuration(config_factory())
    roster = load_roster(roster_factory(), config)
    group = build_group_resources(config, roster)[0]
    team = fake_client.add_team(group.team_name, team_id=42)
    repository = fake_client.add_repository(group.repositories[0].name)
    if current_permission is not None:
        fake_client.permissions[(team.slug, repository.name)] = current_permission
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
        action
        for action in plan.actions
        if action.repository == repository.name
        and action.scope == group.key
        and action.action_id.startswith(f"{group.key}:permission:")
    )

    assert permission_action.action_type == expected_action
    if current_permission is not None:
        assert snapshot.permissions[permission_key(team.slug, repository.name)] == Permission(
            current_permission
        )


@pytest.mark.parametrize(
    ("ledger_status", "retry", "expected_type", "expected_status"),
    [
        (
            InvitationState.EXPIRED,
            False,
            ActionType.REVIEW_REQUIRED,
            ActionStatus.REVIEW,
        ),
        (
            InvitationState.EXPIRED,
            True,
            ActionType.SEND_INVITATION,
            ActionStatus.PLANNED,
        ),
        (
            None,
            True,
            ActionType.SKIP_UNCHANGED,
            ActionStatus.SKIPPED,
        ),
        (
            InvitationState.UNRESOLVED,
            True,
            ActionType.REVIEW_REQUIRED,
            ActionStatus.REVIEW,
        ),
    ],
)
def test_retry_plan_only_makes_explicit_expiry_eligible(
    config_factory,
    record_factory,
    fixed_now,
    ledger_status,
    retry,
    expected_type,
    expected_status,
) -> None:
    config = load_configuration(config_factory())
    student = _student()
    group = _group(student)
    team = Team(id=42, name=group.team_name, slug="expected-team")
    repository = Repository(name=group.repositories[0].name)
    records = (
        [
            record_factory(
                email=student.email,
                status=ledger_status,
                team_id=team.id,
            )
        ]
        if ledger_status is not None
        else []
    )
    snapshot = Snapshot(
        teams=[team],
        repositories=[repository],
        pending_invitations=[],
        failed_invitations=[],
        team_members={team.slug: set()},
        permissions={
            permission_key(team.slug, repository.name): Permission.PUSH,
        },
        ledger=InvitationLedger(organisation=config.organisation, records=records),
    )

    plan = build_provision_plan(
        config,
        [group],
        snapshot,
        mode="Dry run",
        generated_at=fixed_now,
        retry_expired=retry,
        provision_resources=not retry,
    )
    invitation_action = next(
        action for action in plan.actions if action.student_id == student.student_id
    )

    assert invitation_action.action_type == expected_type
    assert invitation_action.status == expected_status
