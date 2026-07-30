from __future__ import annotations

import pytest

from gh_edu.core import (
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    EXIT_VALIDATION,
    ActionStatus,
    ActionType,
    InputValidationError,
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
    save_ledger_atomic,
    verify_execution,
)
from gh_edu.github import GitHubError, GitHubNetworkError


def _invitation_calls(fake_client):
    return [
        call for call in fake_client.write_calls if call.operation == "invite_member"
    ]


def _saved_ledger(config_path):
    config = load_configuration(config_path)
    return load_ledger(ledger_path(config_path, config), config.organisation)


def test_individual_resource_builders_use_existing_roster_and_create_no_repositories(
    config_factory,
    roster_factory,
) -> None:
    config = load_configuration(config_factory())
    individual_roster = load_roster(
        roster_factory(
            [
                {
                    "student_id": "00123456",
                    "email": "00123456@student.example.edu.au",
                    "group_id": "IND-00123456",
                },
                {
                    "student_id": "87654321",
                    "email": "87654321@student.example.edu.au",
                    "group_id": "IND-87654321",
                },
            ]
        ),
        config,
    )

    individual_groups = build_individual_resources(config, individual_roster)

    assert [group.group_id for group in individual_groups] == [
        "IND-00123456",
        "IND-87654321",
    ]
    assert [group.team_name for group in individual_groups] == [
        "IND-00123456",
        "IND-87654321",
    ]
    assert all(group.individual for group in individual_groups)
    assert all(group.repositories == [] for group in individual_groups)
    assert all(len(group.students) == 1 for group in individual_groups)

    group_roster = load_roster(
        roster_factory(
            [
                {
                    "student_id": "00123456",
                    "email": "00123456@student.example.edu.au",
                    "group_id": "G01",
                },
                {
                    "student_id": "87654321",
                    "email": "87654321@student.example.edu.au",
                    "group_id": "G01",
                },
            ]
        ),
        config,
    )
    combined = build_group_resources(config, group_roster, add_individual=True)
    shared = [group for group in combined if not group.individual]
    individual = [group for group in combined if group.individual]

    assert len(shared) == 1
    assert [repository.name for repository in shared[0].repositories] == [
        "COMP3018-2026S2-G01"
    ]
    assert {group.team_name for group in individual} == {
        "IND-00123456",
        "IND-87654321",
    }
    assert all(group.repositories == [] for group in individual)


def test_individual_roster_requires_exact_ind_student_marker(
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
                    "group_id": "IND-123456",
                }
            ]
        ),
        config,
    )

    with pytest.raises(InputValidationError, match="IND-00123456"):
        build_individual_resources(config, roster)


def test_plural_individuals_derives_group_id_when_column_is_absent(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory(
        [
            {
                "student_id": "00123456",
                "email": "00123456@student.example.edu.au",
            },
            {
                "student_id": "87654321",
                "email": "87654321@student.example.edu.au",
            },
        ],
        headers=["student_id", "email"],
    )

    result = invoke_cli(
        fake_client,
        [
            "provision",
            "individuals",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--apply",
        ],
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert set(fake_client.teams) == {"IND-00123456", "IND-87654321"}
    assert {
        record.group_id for record in _saved_ledger(config_path).records
    } == {"IND-00123456", "IND-87654321"}


def test_group_provisioning_still_requires_group_id(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory(
        [
            {
                "student_id": "00123456",
                "email": "00123456@student.example.edu.au",
            }
        ],
        headers=["student_id", "email"],
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

    assert result.exit_code == EXIT_VALIDATION
    assert "missing required column(s): group_id" in result.output
    assert not fake_client.calls


def test_cli_exposes_plural_individuals_and_opt_in_flags(runner) -> None:
    from gh_edu.cli import app

    provision = runner.invoke(app, ["provision", "--help"])
    groups = runner.invoke(app, ["provision", "groups", "--help"])
    individuals = runner.invoke(app, ["provision", "individuals", "--help"])
    retry = runner.invoke(app, ["invitations", "retry-expired", "--help"])

    assert provision.exit_code == groups.exit_code == individuals.exit_code == retry.exit_code == 0
    assert "individuals" in provision.stdout
    assert "--add-individual" in groups.stdout
    assert "--add-repository" in individuals.stdout
    assert "--add-individual" in retry.stdout


def test_plural_individual_apply_is_team_only_and_idempotent(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    rows = [
        {
            "student_id": "00123456",
            "email": "00123456@student.example.edu.au",
            "group_id": "IND-00123456",
        },
        {
            "student_id": "87654321",
            "email": "87654321@student.example.edu.au",
            "group_id": "IND-87654321",
        },
    ]
    roster_path = roster_factory(rows)
    command = [
        "provision",
        "individuals",
        "--config",
        str(config_path),
        "--roster",
        str(roster_path),
        "--apply",
    ]

    result = invoke_cli(fake_client, command)

    assert result.exit_code == 0, result.output
    assert set(fake_client.teams) == {"IND-00123456", "IND-87654321"}
    assert fake_client.repositories == {}
    assert not [
        call
        for call in fake_client.write_calls
        if call.operation
        in {
            "create_repository_from_template",
            "set_team_repository_permission",
        }
    ]
    assert not [
        call for call in fake_client.read_calls if call.operation == "get_repository"
    ]
    invitation_calls = _invitation_calls(fake_client)
    assert len(invitation_calls) == 2
    for row in rows:
        invitation_call = next(
            call
            for call in invitation_calls
            if call.target == row["email"].casefold()
        )
        team = fake_client.teams[f"IND-{row['student_id']}"]
        assert invitation_call.payload["team_ids"] == (team.id,)

    ledger = _saved_ledger(config_path)
    assert len(ledger.records) == 2
    assert {record.group_id for record in ledger.records} == {
        "IND-00123456",
        "IND-87654321",
    }
    assert all(record.attempt_count == 1 for record in ledger.records)
    assert list((config_path.parent / "reports").glob("*_individuals-plan.md"))
    assert list((config_path.parent / "reports").glob("*_individuals-apply.md"))

    fake_client.clear_calls()
    repeated = invoke_cli(fake_client, command)

    assert repeated.exit_code == 0, repeated.output
    assert not fake_client.write_calls
    repeated_ledger = _saved_ledger(config_path)
    assert len(repeated_ledger.records) == 2
    assert all(record.attempt_count == 1 for record in repeated_ledger.records)


def test_plural_individual_add_repository_creates_or_reuses_each_exact_repository(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    rows = [
        {
            "student_id": "00123456",
            "email": "00123456@student.example.edu.au",
            "repository": "Capstone.One-00123456",
        },
        {
            "student_id": "87654321",
            "email": "87654321@student.example.edu.au",
            "repository": "legacy_repo.87654321",
        },
    ]
    roster_path = roster_factory(
        rows,
        headers=["student_id", "email", "repository"],
    )
    existing_repository = fake_client.add_repository(rows[1]["repository"])
    command = [
        "provision",
        "individuals",
        "--config",
        str(config_path),
        "--roster",
        str(roster_path),
        "--add-repository",
        "--apply",
    ]

    result = invoke_cli(fake_client, command)

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert set(fake_client.teams) == {"IND-00123456", "IND-87654321"}
    assert set(fake_client.repositories) == {
        "Capstone.One-00123456",
        existing_repository.name,
    }
    create_calls = [
        call
        for call in fake_client.write_calls
        if call.operation == "create_repository_from_template"
    ]
    assert [call.target for call in create_calls] == ["Capstone.One-00123456"]
    permission_calls = [
        call
        for call in fake_client.write_calls
        if call.operation == "set_team_repository_permission"
    ]
    assert {
        call.target: call.payload["permission"]
        for call in permission_calls
    } == {
        "ind-00123456/Capstone.One-00123456": "push",
        "ind-87654321/legacy_repo.87654321": "push",
    }
    assert len(_invitation_calls(fake_client)) == 2

    fake_client.clear_calls()
    repeated = invoke_cli(fake_client, command)

    assert repeated.exit_code == EXIT_SUCCESS, repeated.output
    assert not fake_client.write_calls
    assert fake_client.permissions == {
        ("ind-00123456", "Capstone.One-00123456"): "push",
        ("ind-87654321", "legacy_repo.87654321"): "push",
    }


def test_add_repository_reuses_previously_provisioned_team_without_reinviting(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    student = {
        "student_id": "00123456",
        "email": "00123456@student.example.edu.au",
        "group_id": "IND-00123456",
    }
    team_only_roster = roster_factory([student])
    team_only = invoke_cli(
        fake_client,
        [
            "provision",
            "individuals",
            "--config",
            str(config_path),
            "--roster",
            str(team_only_roster),
            "--apply",
        ],
    )
    assert team_only.exit_code == EXIT_SUCCESS, team_only.output
    existing_team = fake_client.teams["IND-00123456"]

    fake_client.clear_calls()
    repository_roster = roster_factory(
        [{**student, "repository": "Capstone.One-00123456"}],
        headers=["student_id", "email", "group_id", "repository"],
    )
    with_repository = invoke_cli(
        fake_client,
        [
            "provision",
            "individuals",
            "--config",
            str(config_path),
            "--roster",
            str(repository_roster),
            "--add-repository",
            "--apply",
        ],
    )

    assert with_repository.exit_code == EXIT_SUCCESS, with_repository.output
    assert fake_client.teams["IND-00123456"].id == existing_team.id
    assert set(fake_client.repositories) == {"Capstone.One-00123456"}
    assert fake_client.permissions == {
        ("ind-00123456", "Capstone.One-00123456"): "push"
    }
    assert not _invitation_calls(fake_client)
    assert [
        call.operation for call in fake_client.write_calls
    ] == [
        "create_repository_from_template",
        "set_team_repository_permission",
    ]


def test_plural_individual_without_flag_ignores_optional_repository_column(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    rows = [
        {
            "student_id": "00123456",
            "email": "00123456@student.example.edu.au",
            "group_id": "IND-00123456",
            "repository": "not/a-valid-repository",
        },
        {
            "student_id": "87654321",
            "email": "87654321@student.example.edu.au",
            "group_id": "IND-87654321",
            "repository": "not/a-valid-repository",
        },
    ]
    roster_path = roster_factory(
        rows,
        headers=["student_id", "email", "group_id", "repository"],
    )

    result = invoke_cli(
        fake_client,
        [
            "provision",
            "individuals",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--apply",
        ],
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert set(fake_client.teams) == {"IND-00123456", "IND-87654321"}
    assert fake_client.repositories == {}
    assert fake_client.permissions == {}
    assert not [
        call
        for call in fake_client.calls
        if call.operation
        in {
            "get_repository",
            "create_repository_from_template",
            "get_team_repository_permission",
            "set_team_repository_permission",
        }
    ]


@pytest.mark.parametrize(
    ("headers", "row"),
    [
        (
            ["student_id", "email", "group_id"],
            {
                "student_id": "00123456",
                "email": "00123456@student.example.edu.au",
                "group_id": "IND-00123456",
            },
        ),
        (
            ["student_id", "email", "group_id", "Repository"],
            {
                "student_id": "00123456",
                "email": "00123456@student.example.edu.au",
                "group_id": "IND-00123456",
                "Repository": "repo-00123456",
            },
        ),
    ],
    ids=["missing", "wrong-case"],
)
def test_plural_individual_add_repository_requires_exact_repository_header_before_github(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
    headers,
    row,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory([row], headers=headers)

    result = invoke_cli(
        fake_client,
        [
            "provision",
            "individuals",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--add-repository",
            "--apply",
        ],
    )

    assert result.exit_code == EXIT_VALIDATION
    assert "missing required column(s): repository" in result.output
    assert not fake_client.calls


@pytest.mark.parametrize(
    ("repositories", "message"),
    [
        (["", "repo-87654321"], "repository is required"),
        (["invalid/repository", "repo-87654321"], "repository names must"),
        (["shared-repository", "shared-repository"], "same repository"),
    ],
    ids=["blank", "invalid", "duplicate"],
)
def test_plural_individual_add_repository_rejects_bad_assignments_before_github(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
    repositories,
    message,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory(
        [
            {
                "student_id": "00123456",
                "email": "00123456@student.example.edu.au",
                "group_id": "IND-00123456",
                "repository": repositories[0],
            },
            {
                "student_id": "87654321",
                "email": "87654321@student.example.edu.au",
                "group_id": "IND-87654321",
                "repository": repositories[1],
            },
        ],
        headers=["student_id", "email", "group_id", "repository"],
    )

    result = invoke_cli(
        fake_client,
        [
            "provision",
            "individuals",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--add-repository",
            "--apply",
        ],
    )

    assert result.exit_code == EXIT_VALIDATION
    assert message in result.output
    assert not fake_client.calls


def test_group_add_individual_sends_one_two_team_invite_without_individual_repo_access(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
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
    ]
    roster_path = roster_factory(rows)
    command = [
        "provision",
        "groups",
        "--config",
        str(config_path),
        "--roster",
        str(roster_path),
        "--add-individual",
        "--apply",
    ]

    result = invoke_cli(fake_client, command)

    assert result.exit_code == 0, result.output
    shared_team = fake_client.teams["COMP3018-2026S2-G01"]
    assert set(fake_client.teams) == {
        shared_team.name,
        "IND-10000001",
        "IND-10000002",
    }
    assert set(fake_client.repositories) == {"COMP3018-2026S2-G01"}
    permission_calls = [
        call
        for call in fake_client.write_calls
        if call.operation == "set_team_repository_permission"
    ]
    assert len(permission_calls) == 1
    assert permission_calls[0].target == (
        f"{shared_team.slug}/COMP3018-2026S2-G01"
    )

    invitation_calls = _invitation_calls(fake_client)
    assert len(invitation_calls) == 2
    for row in rows:
        invitation_call = next(
            call
            for call in invitation_calls
            if call.target == row["email"].casefold()
        )
        individual_team = fake_client.teams[f"IND-{row['student_id']}"]
        assert invitation_call.payload["team_ids"] == (
            shared_team.id,
            individual_team.id,
        )

    ledger = _saved_ledger(config_path)
    assert len(ledger.records) == 4
    for row in rows:
        records = [
            record
            for record in ledger.records
            if record.email.casefold() == row["email"].casefold()
        ]
        assert {record.team_name for record in records} == {
            shared_team.name,
            f"IND-{row['student_id']}",
        }
        assert len({record.invitation_id for record in records}) == 1
        assert all(record.attempt_count == 1 for record in records)

    apply_report = next(
        (config_path.parent / "reports").glob("*_provision-apply.md")
    ).read_text(encoding="utf-8")
    assert shared_team.name in apply_report
    assert "IND-10000001" in apply_report
    assert "IND-10000002" in apply_report

    fake_client.clear_calls()
    repeated = invoke_cli(fake_client, command)

    assert repeated.exit_code == 0, repeated.output
    assert not fake_client.write_calls
    repeated_ledger = _saved_ledger(config_path)
    assert len(repeated_ledger.records) == 4
    assert all(record.attempt_count == 1 for record in repeated_ledger.records)


def test_group_without_flag_retains_single_shared_team_invitation(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()

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

    assert result.exit_code == 0, result.output
    assert set(fake_client.teams) == {"COMP3018-2026S2-G01"}
    invitation_call = _invitation_calls(fake_client)
    assert len(invitation_call) == 1
    assert invitation_call[0].payload["team_ids"] == (
        fake_client.teams["COMP3018-2026S2-G01"].id,
    )
    assert len(_saved_ledger(config_path).records) == 1


def test_group_only_pending_invitation_creates_individual_team_but_never_resends(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    row = {
        "student_id": "12345678",
        "email": "12345678@student.example.edu.au",
        "group_id": "G01",
    }
    roster_path = roster_factory([row])
    shared_team = fake_client.add_team("COMP3018-2026S2-G01", team_id=41)
    repository = fake_client.add_repository("COMP3018-2026S2-G01")
    fake_client.permissions[(shared_team.slug, repository.name)] = "push"
    fake_client.add_pending(row["email"], invitation_id=99, team_ids=[shared_team.id])

    result = invoke_cli(
        fake_client,
        [
            "provision",
            "groups",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--add-individual",
            "--apply",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Review required: 1" in result.stdout
    assert "IND-12345678" in fake_client.teams
    assert not _invitation_calls(fake_client)
    assert not [
        call
        for call in fake_client.write_calls
        if call.operation == "set_team_repository_permission"
        and call.target.startswith(fake_client.teams["IND-12345678"].slug)
    ]
    assert fake_client.pending[row["email"].casefold()].team_ids == (shared_team.id,)
    records = _saved_ledger(config_path).records
    assert len(records) == 2
    records_by_team = {record.team_name: record for record in records}
    assert records_by_team[shared_team.name].status == InvitationState.PENDING
    assert records_by_team["IND-12345678"].status == InvitationState.FAILED
    assert len({record.invitation_id for record in records}) == 1


def test_combined_expired_invitation_requires_flag_and_retries_both_teams_once(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
    record_factory,
) -> None:
    config_path = config_factory()
    config = load_configuration(config_path)
    row = {
        "student_id": "12345678",
        "email": "12345678@student.example.edu.au",
        "group_id": "G01",
    }
    roster_path = roster_factory([row])
    shared_team = fake_client.add_team("COMP3018-2026S2-G01", team_id=41)
    individual_team = fake_client.add_team("IND-12345678", team_id=42)
    repository = fake_client.add_repository("COMP3018-2026S2-G01")
    fake_client.permissions[(shared_team.slug, repository.name)] = "push"
    ledger_file = ledger_path(config_path, config)
    save_ledger_atomic(
        ledger_file,
        InvitationLedger(
            organisation=config.organisation,
            records=[
                record_factory(
                    email=row["email"],
                    group_id="G01",
                    team_name=shared_team.name,
                    team_id=shared_team.id,
                    invitation_id=2999,
                    status=InvitationState.EXPIRED,
                ),
                record_factory(
                    email=row["email"],
                    group_id="IND-12345678",
                    team_name=individual_team.name,
                    team_id=individual_team.id,
                    invitation_id=2999,
                    status=InvitationState.EXPIRED,
                ),
            ],
        ),
    )
    base_command = [
        "invitations",
        "retry-expired",
        "--config",
        str(config_path),
        "--roster",
        str(roster_path),
        "--apply",
    ]

    without_flag = invoke_cli(fake_client, base_command)

    assert without_flag.exit_code == 0, without_flag.output
    assert not _invitation_calls(fake_client)
    assert all(
        record.invitation_id == 2999
        for record in load_ledger(ledger_file, config.organisation).records
    )

    fake_client.clear_calls()
    with_flag = invoke_cli(fake_client, [*base_command, "--add-individual"])

    assert with_flag.exit_code == 0, with_flag.output
    invitation_calls = _invitation_calls(fake_client)
    assert len(invitation_calls) == 1
    assert invitation_calls[0].payload["team_ids"] == (
        shared_team.id,
        individual_team.id,
    )
    updated = load_ledger(ledger_file, config.organisation)
    assert len(updated.records) == 2
    assert {record.status for record in updated.records} == {
        InvitationState.PENDING
    }
    assert {record.attempt_count for record in updated.records} == {2}
    assert len({record.invitation_id for record in updated.records}) == 1
    assert next(iter({record.invitation_id for record in updated.records})) != 2999


def test_combined_pending_invitation_is_adopted_into_two_ledger_records(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    config_path = config_factory()
    config = load_configuration(config_path)
    roster = load_roster(roster_factory(), config)
    groups = build_group_resources(config, roster, add_individual=True)
    shared_group = next(group for group in groups if not group.individual)
    individual_group = next(group for group in groups if group.individual)
    shared_team = fake_client.add_team(shared_group.team_name, team_id=41)
    individual_team = fake_client.add_team(individual_group.team_name, team_id=42)
    repository = fake_client.add_repository(shared_group.repositories[0].name)
    fake_client.permissions[(shared_team.slug, repository.name)] = "push"
    student = shared_group.students[0]
    invitation = fake_client.add_pending(
        student.email,
        invitation_id=99,
        team_ids=[shared_team.id, individual_team.id],
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

    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
        now=lambda: fixed_now,
    )

    assert outcome.exit_code == EXIT_SUCCESS
    assert not fake_client.write_calls
    records = load_ledger(ledger_file, config.organisation).records
    assert len(records) == 2
    assert {record.team_id for record in records} == {
        shared_team.id,
        individual_team.id,
    }
    assert {record.invitation_id for record in records} == {invitation.id}
    assert {record.status for record in records} == {InvitationState.PENDING}


def test_combined_acceptance_requires_same_login_in_both_teams(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    config = load_configuration(
        config_factory(
            overrides={"roster": {"github_login_column": "github_login"}}
        )
    )
    roster = load_roster(
        roster_factory(
            [
                {
                    "student_id": "12345678",
                    "email": "12345678@student.example.edu.au",
                    "group_id": "G01",
                    "github_login": "known-login",
                }
            ],
            headers=["student_id", "email", "group_id", "github_login"],
        ),
        config,
    )
    groups = build_group_resources(config, roster, add_individual=True)
    shared_group = next(group for group in groups if not group.individual)
    individual_group = next(group for group in groups if group.individual)
    shared_team = fake_client.add_team(shared_group.team_name, team_id=41)
    individual_team = fake_client.add_team(individual_group.team_name, team_id=42)
    repository = fake_client.add_repository(shared_group.repositories[0].name)
    fake_client.permissions[(shared_team.slug, repository.name)] = "push"
    fake_client.members[shared_team.slug].add("known-login")
    fake_client.members[individual_team.slug].add("known-login")
    ledger = InvitationLedger(organisation=config.organisation)

    complete_snapshot = discover_snapshot(fake_client, config, groups, ledger)
    complete_plan = build_provision_plan(
        config,
        groups,
        complete_snapshot,
        mode="Dry run",
        generated_at=fixed_now,
    )
    complete_action = next(
        action for action in complete_plan.actions if action.student_id
    )

    assert complete_action.action_type == ActionType.SKIP_ACCEPTED
    assert complete_action.invitation_state == InvitationState.ACCEPTED_CONFIRMED

    fake_client.members[individual_team.slug].clear()
    partial_snapshot = discover_snapshot(fake_client, config, groups, ledger)
    partial_plan = build_provision_plan(
        config,
        groups,
        partial_snapshot,
        mode="Dry run",
        generated_at=fixed_now,
    )
    partial_action = next(
        action for action in partial_plan.actions if action.student_id
    )

    assert partial_action.action_type == ActionType.REVIEW_REQUIRED
    assert partial_action.status == ActionStatus.REVIEW
    assert partial_action.invitation_state == InvitationState.UNRESOLVED


@pytest.mark.parametrize("complete_remote_bundle", [True, False])
def test_combined_network_recovery_requires_every_expected_team(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
    complete_remote_bundle,
) -> None:
    config_path = config_factory()
    config = load_configuration(config_path)
    roster = load_roster(roster_factory(), config)
    groups = build_group_resources(config, roster, add_individual=True)
    shared_group = next(group for group in groups if not group.individual)
    individual_group = next(group for group in groups if group.individual)
    shared_team = fake_client.add_team(shared_group.team_name, team_id=41)
    individual_team = fake_client.add_team(individual_group.team_name, team_id=42)
    repository = fake_client.add_repository(shared_group.repositories[0].name)
    fake_client.permissions[(shared_team.slug, repository.name)] = "push"
    student = shared_group.students[0]
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
    attached_ids = [shared_team.id]
    if complete_remote_bundle:
        attached_ids.append(individual_team.id)
    fake_client.add_pending(
        student.email,
        invitation_id=79,
        team_ids=attached_ids,
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
    records = load_ledger(ledger_file, config.organisation).records

    if complete_remote_bundle:
        assert outcome.exit_code == EXIT_SUCCESS
        assert len(records) == 2
        assert {record.status for record in records} == {
            InvitationState.PENDING
        }
        assert {record.invitation_id for record in records} == {79}
    else:
        assert outcome.exit_code == EXIT_PARTIAL
        assert len(records) == 2
        assert {record.status for record in records} == {
            InvitationState.FAILED
        }
        assert {record.invitation_id for record in records} == {None}


def test_individual_team_failure_blocks_only_its_student_invitation(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
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
    ]
    roster_path = roster_factory(rows)
    fake_client.fail_next(
        "create_team",
        GitHubError("individual team creation failed"),
        target="IND-10000001",
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
            "--add-individual",
            "--apply",
        ],
    )

    assert result.exit_code == EXIT_PARTIAL
    invitation_calls = _invitation_calls(fake_client)
    assert len(invitation_calls) == 1
    assert invitation_calls[0].target == "two@student.example.edu.au"
    assert {
        record.student_id for record in _saved_ledger(config_path).records
    } == {"10000002"}


def test_post_apply_verification_rejects_partial_team_bundle(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
) -> None:
    config_path = config_factory()
    config = load_configuration(config_path)
    roster = load_roster(roster_factory(), config)
    groups = build_group_resources(config, roster, add_individual=True)
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
    student = next(group for group in groups if not group.individual).students[0]
    invitation = fake_client.pending[student.email.casefold()]
    shared_team = fake_client.teams["COMP3018-2026S2-G01"]
    fake_client.add_pending(
        student.email,
        invitation_id=invitation.id,
        team_ids=[shared_team.id],
    )

    verified = verify_execution(outcome, client=fake_client, config=config)

    assert verified.exit_code == EXIT_PARTIAL
    verification_error = next(
        action
        for action in verified.plan.actions
        if action.action_id.startswith("verify:")
    )
    assert "missing expected team IDs" in verification_error.reason
