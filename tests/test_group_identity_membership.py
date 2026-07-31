from __future__ import annotations

from gh_edu.core import (
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    ActionStatus,
    ActionType,
    InvitationLedger,
    InvitationState,
    build_group_resources,
    build_individual_resources,
    build_provision_plan,
    discover_snapshot,
    execute_plan,
    ledger_path,
    load_configuration,
    load_ledger,
    load_roster,
    render_plan_report,
    verify_execution,
)
from gh_edu.github import GitHubError, GitHubNetworkError


def _ready_group(
    config_factory,
    roster_factory,
    fake_client,
    *,
    rows=None,
    add_individual: bool = False,
    config_overrides=None,
    headers=None,
):
    config_path = config_factory(overrides=config_overrides)
    roster_path = roster_factory(rows, headers=headers)
    config = load_configuration(config_path)
    roster = load_roster(roster_path, config)
    groups = build_group_resources(
        config,
        roster,
        add_individual=add_individual,
    )
    shared_group = next(group for group in groups if not group.individual)
    shared_team = fake_client.add_team(shared_group.team_name, team_id=41)
    repository = fake_client.add_repository(shared_group.repositories[0].name)
    fake_client.permissions[(shared_team.slug, repository.name)] = "push"
    identities = build_individual_resources(
        config,
        roster,
        require_group_marker=False,
    )
    return (
        config_path,
        config,
        roster,
        groups,
        shared_group,
        shared_team,
        identities,
    )


def _student_action(plan, student_id: str):
    return next(
        action
        for action in plan.actions
        if action.student_id == student_id
    )


def test_accepted_individual_identity_adds_member_without_invitation(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    (
        config_path,
        config,
        roster,
        groups,
        _shared_group,
        shared_team,
        identities,
    ) = _ready_group(config_factory, roster_factory, fake_client)
    student = roster.students[0]
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    identity_member = fake_client.add_member(
        identity_team.slug,
        "accepted-student",
        user_id=7001,
    )
    ledger = InvitationLedger(organisation=config.organisation)
    ledger_file = ledger_path(config_path, config)
    snapshot = discover_snapshot(fake_client, config, groups, ledger)
    plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )
    action = _student_action(plan, student.student_id)

    assert action.action_type == ActionType.ADD_TEAM_MEMBER
    assert action.github_user_id == identity_member.id
    assert action.github_login == identity_member.login
    assert action.identity_team_id == identity_team.id
    assert not [
        planned
        for planned in plan.actions
        if planned.action_type == ActionType.SEND_INVITATION
    ]
    report = render_plan_report(plan)
    assert "Add team members" in report
    assert "GitHub user ID: `7001`" in report
    assert f"Identity team: `{identity_team.name}`" in report

    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )
    verified = verify_execution(outcome, client=fake_client, config=config)

    assert verified.exit_code == EXIT_SUCCESS
    assert verified.successful_writes == 1
    membership_calls = [
        call
        for call in fake_client.write_calls
        if call.operation == "add_team_member"
    ]
    assert len(membership_calls) == 1
    assert membership_calls[0].target == (
        f"{shared_team.slug}/{identity_member.login.casefold()}"
    )
    assert not [
        call
        for call in fake_client.write_calls
        if call.operation == "invite_member"
    ]
    assert {
        member.id
        for member in fake_client.members[shared_team.slug]
    } == {identity_member.id}
    assert load_ledger(ledger_file, config.organisation).records == []


def test_groups_cli_applies_resolved_membership_and_reports_it(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config = load_configuration(config_path)
    roster = load_roster(roster_path, config)
    groups = build_group_resources(config, roster)
    shared_group = groups[0]
    shared_team = fake_client.add_team(shared_group.team_name, team_id=41)
    repository = fake_client.add_repository(shared_group.repositories[0].name)
    fake_client.permissions[(shared_team.slug, repository.name)] = "push"
    identity = build_individual_resources(
        config,
        roster,
        require_group_marker=False,
    )[0]
    identity_team = fake_client.add_team(identity.team_name, team_id=51)
    fake_client.add_member(
        identity_team.slug,
        "accepted-student",
        user_id=7001,
    )

    result = invoke_cli(
        fake_client,
        [
            "provision",
            "groups",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--apply",
        ],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert "Add team members: 1" in result.stdout
    assert len(
        [
            call
            for call in fake_client.write_calls
            if call.operation == "add_team_member"
        ]
    ) == 1
    assert not [
        call
        for call in fake_client.write_calls
        if call.operation == "invite_member"
    ]


def test_existing_shared_membership_is_skipped_by_numeric_user_id(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    (
        _config_path,
        config,
        roster,
        groups,
        _shared_group,
        shared_team,
        identities,
    ) = _ready_group(config_factory, roster_factory, fake_client)
    student = roster.students[0]
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    fake_client.add_member(
        identity_team.slug,
        "Student-Login",
        user_id=7001,
    )
    fake_client.add_member(
        shared_team.slug,
        "student-login",
        user_id=7001,
        role="maintainer",
    )

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
    action = _student_action(plan, student.student_id)

    assert action.action_type == ActionType.SKIP_ACCEPTED
    assert action.status == ActionStatus.SKIPPED
    assert action.github_user_id == 7001
    assert "same numeric GitHub user ID" in action.reason


def test_missing_individual_team_preserves_ordinary_group_invitation(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    (
        _config_path,
        config,
        roster,
        groups,
        _shared_group,
        _shared_team,
        _identities,
    ) = _ready_group(config_factory, roster_factory, fake_client)

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
    action = _student_action(plan, roster.students[0].student_id)

    assert action.action_type == ActionType.SEND_INVITATION
    assert action.status == ActionStatus.PLANNED


def test_existing_but_unresolved_individual_teams_never_trigger_group_invites(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    scenarios = {
        "empty": [],
        "multiple": [
            ("first-student", 7001, "member", False),
            ("second-student", 7002, "member", False),
        ],
        "maintainer-only": [
            ("course-maintainer", 7003, "maintainer", False),
        ],
        "inherited-only": [
            ("inherited-student", 7004, "member", True),
        ],
    }

    for index, (scenario, members) in enumerate(scenarios.items(), start=1):
        local_fake = type(fake_client)()
        (
            _config_path,
            config,
            roster,
            groups,
            _shared_group,
            _shared_team,
            identities,
        ) = _ready_group(config_factory, roster_factory, local_fake)
        identity_team = local_fake.add_team(
            identities[0].team_name,
            team_id=50 + index,
        )
        for login, user_id, role, inherited in members:
            local_fake.add_member(
                identity_team.slug,
                login,
                user_id=user_id,
                role=role,
                inherited=inherited,
            )

        snapshot = discover_snapshot(
            local_fake,
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
        action = _student_action(plan, roster.students[0].student_id)

        assert action.action_type == ActionType.REVIEW_REQUIRED, scenario
        assert action.status == ActionStatus.REVIEW, scenario
        assert not [
            planned
            for planned in plan.actions
            if planned.action_type == ActionType.SEND_INVITATION
        ], scenario


def test_missing_individual_team_with_ledger_history_requires_review(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
    record_factory,
) -> None:
    (
        _config_path,
        config,
        roster,
        groups,
        _shared_group,
        _shared_team,
        identities,
    ) = _ready_group(config_factory, roster_factory, fake_client)
    student = roster.students[0]
    identity = identities[0]
    ledger = InvitationLedger(
        organisation=config.organisation,
        records=[
            record_factory(
                student_id=student.student_id,
                email=student.email,
                group_id=identity.group_id,
                team_name=identity.team_name,
                team_id=51,
                status=InvitationState.PENDING,
            )
        ],
    )

    snapshot = discover_snapshot(fake_client, config, groups, ledger)
    plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Dry run",
        generated_at=fixed_now,
    )
    action = _student_action(plan, student.student_id)

    assert action.action_type == ActionType.REVIEW_REQUIRED
    assert "ledger records an individual-team assignment" in action.reason


def test_complete_group_pending_invitation_remains_skipped_when_identity_is_empty(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    (
        _config_path,
        config,
        roster,
        groups,
        _shared_group,
        shared_team,
        identities,
    ) = _ready_group(config_factory, roster_factory, fake_client)
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    student = roster.students[0]
    fake_client.add_pending(
        student.email,
        team_ids=[shared_team.id],
    )

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
    action = _student_action(plan, student.student_id)

    assert identity_team.id not in action.pending_team_ids
    assert action.action_type == ActionType.SKIP_PENDING_INVITATION
    assert action.status == ActionStatus.SKIPPED


def test_individual_only_pending_invitation_requires_review_for_group_assignment(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    (
        _config_path,
        config,
        roster,
        groups,
        _shared_group,
        _shared_team,
        identities,
    ) = _ready_group(config_factory, roster_factory, fake_client)
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    student = roster.students[0]
    fake_client.add_pending(
        student.email,
        team_ids=[identity_team.id],
    )

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
    action = _student_action(plan, student.student_id)

    assert action.action_type == ActionType.REVIEW_REQUIRED
    assert action.status == ActionStatus.REVIEW
    assert not [
        planned
        for planned in plan.actions
        if planned.action_type == ActionType.SEND_INVITATION
    ]


def test_add_individual_uses_direct_membership_for_an_accepted_identity(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    (
        _config_path,
        config,
        roster,
        groups,
        _shared_group,
        _shared_team,
        identities,
    ) = _ready_group(
        config_factory,
        roster_factory,
        fake_client,
        add_individual=True,
    )
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    fake_client.add_member(
        identity_team.slug,
        "accepted-student",
        user_id=7001,
    )

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
    action = _student_action(plan, roster.students[0].student_id)

    assert action.action_type == ActionType.ADD_TEAM_MEMBER
    assert action.github_user_id == 7001
    assert not [
        planned
        for planned in plan.actions
        if planned.action_type == ActionType.SEND_INVITATION
    ]


def test_verified_login_mismatch_requires_review(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    rows = [
        {
            "student_id": "12345678",
            "email": "student@example.edu.au",
            "group_id": "G01",
            "github_login": "expected-login",
        }
    ]
    (
        _config_path,
        config,
        roster,
        groups,
        _shared_group,
        _shared_team,
        identities,
    ) = _ready_group(
        config_factory,
        roster_factory,
        fake_client,
        rows=rows,
        config_overrides={
            "roster": {"github_login_column": "github_login"}
        },
        headers=["student_id", "email", "group_id", "github_login"],
    )
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    fake_client.add_member(
        identity_team.slug,
        "different-login",
        user_id=7001,
    )

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
    action = _student_action(plan, roster.students[0].student_id)

    assert action.action_type == ActionType.REVIEW_REQUIRED
    assert "verified GitHub login" in action.reason


def test_execution_accepts_login_rename_when_numeric_user_id_is_unchanged(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    (
        config_path,
        config,
        roster,
        groups,
        _shared_group,
        _shared_team,
        identities,
    ) = _ready_group(config_factory, roster_factory, fake_client)
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    fake_client.add_member(
        identity_team.slug,
        "old-login",
        user_id=7001,
    )
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(fake_client, config, groups, ledger)
    plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )
    fake_client.members[identity_team.slug].clear()
    fake_client.add_member(
        identity_team.slug,
        "renamed-login",
        user_id=7001,
    )

    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_path(config_path, config),
        now=lambda: fixed_now,
    )
    action = _student_action(outcome.plan, roster.students[0].student_id)

    assert outcome.exit_code == EXIT_SUCCESS
    assert action.status == ActionStatus.SUCCEEDED
    assert action.github_login == "renamed-login"
    membership_call = next(
        call
        for call in fake_client.write_calls
        if call.operation == "add_team_member"
    )
    assert membership_call.target.endswith("/renamed-login")


def test_execution_refuses_changed_identity_user_id_without_membership_write(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    (
        config_path,
        config,
        roster,
        groups,
        _shared_group,
        _shared_team,
        identities,
    ) = _ready_group(config_factory, roster_factory, fake_client)
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    fake_client.add_member(
        identity_team.slug,
        "original-login",
        user_id=7001,
    )
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(fake_client, config, groups, ledger)
    plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )
    fake_client.members[identity_team.slug].clear()
    fake_client.add_member(
        identity_team.slug,
        "different-student",
        user_id=7002,
    )

    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_path(config_path, config),
        now=lambda: fixed_now,
    )
    action = _student_action(outcome.plan, roster.students[0].student_id)

    assert outcome.exit_code == EXIT_PARTIAL
    assert action.status == ActionStatus.FAILED
    assert "same sole direct active" in (action.error or "")
    assert not [
        call
        for call in fake_client.write_calls
        if call.operation == "add_team_member"
    ]


def test_pending_membership_response_is_rejected(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    (
        config_path,
        config,
        roster,
        groups,
        _shared_group,
        _shared_team,
        identities,
    ) = _ready_group(config_factory, roster_factory, fake_client)
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    fake_client.add_member(
        identity_team.slug,
        "accepted-student",
        user_id=7001,
    )
    fake_client.pending_membership_logins.add("accepted-student")
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
    action = _student_action(outcome.plan, roster.students[0].student_id)

    assert outcome.exit_code == EXIT_PARTIAL
    assert action.status == ActionStatus.FAILED
    assert "pending team membership" in (action.error or "")


def test_shared_resource_failure_blocks_dependent_membership(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    (
        config_path,
        config,
        roster,
        groups,
        shared_group,
        _shared_team,
        identities,
    ) = _ready_group(config_factory, roster_factory, fake_client)
    fake_client.repositories.clear()
    fake_client.permissions.clear()
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    fake_client.add_member(
        identity_team.slug,
        "accepted-student",
        user_id=7001,
    )
    fake_client.fail_next(
        "create_repository_from_template",
        GitHubError("template generation failed"),
        target=shared_group.repositories[0].name,
    )
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
    action = _student_action(outcome.plan, roster.students[0].student_id)

    assert outcome.exit_code == EXIT_PARTIAL
    assert action.action_type == ActionType.ADD_TEAM_MEMBER
    assert action.status == ActionStatus.BLOCKED
    assert not [
        call
        for call in fake_client.write_calls
        if call.operation == "add_team_member"
    ]


def test_group_membership_is_additive_and_does_not_remove_old_group(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    rows = [
        {
            "student_id": "12345678",
            "email": "student@example.edu.au",
            "group_id": "G02",
        }
    ]
    (
        config_path,
        config,
        _roster,
        groups,
        _shared_group,
        _shared_team,
        identities,
    ) = _ready_group(
        config_factory,
        roster_factory,
        fake_client,
        rows=rows,
    )
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    fake_client.add_member(identity_team.slug, "student-login", user_id=7001)
    old_group = fake_client.add_team("COMP3018-2026S2-G01", team_id=40)
    fake_client.add_member(old_group.slug, "student-login", user_id=7001)
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

    assert outcome.exit_code == EXIT_SUCCESS
    assert {member.id for member in fake_client.members[old_group.slug]} == {
        7001
    }
    assert not [
        call
        for call in fake_client.write_calls
        if call.method == "DELETE"
    ]


def test_network_error_recovers_only_from_matching_numeric_membership(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
    monkeypatch,
) -> None:
    (
        config_path,
        config,
        roster,
        groups,
        _shared_group,
        _shared_team,
        identities,
    ) = _ready_group(config_factory, roster_factory, fake_client)
    identity_team = fake_client.add_team(identities[0].team_name, team_id=51)
    fake_client.add_member(
        identity_team.slug,
        "accepted-student",
        user_id=7001,
    )
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(fake_client, config, groups, ledger)
    plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )

    def ambiguous_membership_write(org: str, slug: str, username: str):
        fake_client._record(
            "add_team_member",
            "PUT",
            f"{slug}/{username.casefold()}",
            {"role": "member"},
        )
        fake_client.add_member(slug, username, user_id=7001)
        raise GitHubNetworkError("connection reset")

    monkeypatch.setattr(
        fake_client,
        "add_team_member",
        ambiguous_membership_write,
    )

    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_path(config_path, config),
        now=lambda: fixed_now,
    )
    action = _student_action(outcome.plan, roster.students[0].student_id)

    assert outcome.exit_code == EXIT_SUCCESS
    assert action.status == ActionStatus.SUCCEEDED
    assert "network error" in action.reason


def test_mixed_group_roster_uses_identity_or_invitation_per_student(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    rows = [
        {
            "student_id": "10000001",
            "email": "one@student.example.edu.au",
            "group_id": "G01",
        },
        {
            "student_id": "10000002",
            "email": "two@student.example.edu.au",
            "group_id": "G01",
        },
        {
            "student_id": "10000003",
            "email": "three@student.example.edu.au",
            "group_id": "G01",
        },
        {
            "student_id": "10000004",
            "email": "four@student.example.edu.au",
            "group_id": "G01",
        },
    ]
    (
        _config_path,
        config,
        _roster,
        groups,
        _shared_group,
        shared_team,
        identities,
    ) = _ready_group(
        config_factory,
        roster_factory,
        fake_client,
        rows=rows,
    )
    identities_by_student = {
        identity.students[0].student_id: identity
        for identity in identities
    }
    first_identity = fake_client.add_team(
        identities_by_student["10000001"].team_name,
        team_id=51,
    )
    fake_client.add_member(first_identity.slug, "first-login", user_id=7001)
    second_identity = fake_client.add_team(
        identities_by_student["10000002"].team_name,
        team_id=52,
    )
    fake_client.add_member(second_identity.slug, "second-login", user_id=7002)
    fake_client.add_member(shared_team.slug, "second-login", user_id=7002)
    fake_client.add_team(
        identities_by_student["10000004"].team_name,
        team_id=54,
    )

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

    assert _student_action(plan, "10000001").action_type == (
        ActionType.ADD_TEAM_MEMBER
    )
    assert _student_action(plan, "10000002").action_type == (
        ActionType.SKIP_ACCEPTED
    )
    assert _student_action(plan, "10000003").action_type == (
        ActionType.SEND_INVITATION
    )
    assert _student_action(plan, "10000004").action_type == (
        ActionType.REVIEW_REQUIRED
    )
