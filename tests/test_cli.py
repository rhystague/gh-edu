from __future__ import annotations

from types import SimpleNamespace

import pytest

from gh_edu.core import ExecutionProgress, load_configuration, reports_path
from gh_edu.github import GitHubAuthError, GitHubError, GitHubRateLimitError


def test_help_exposes_required_surface_and_no_supervisor_command(
    runner,
) -> None:
    from gh_edu.cli import app

    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("auth", "roster", "status", "provision", "invitations", "semester"):
        assert command in result.stdout
    assert "supervisor" not in result.stdout.casefold()


def test_nested_command_help_contains_all_workflows(runner) -> None:
    from gh_edu.cli import app

    provision = runner.invoke(app, ["provision", "--help"])
    invitations = runner.invoke(app, ["invitations", "--help"])
    semester = runner.invoke(app, ["semester", "--help"])

    assert provision.exit_code == invitations.exit_code == semester.exit_code == 0
    assert "groups" in provision.stdout
    assert "individual" in provision.stdout
    assert "retry-expired" in invitations.stdout
    assert "close" in semester.stdout


def test_roster_validate_help_exposes_modes_and_repository_option(runner) -> None:
    from gh_edu.cli import app

    result = runner.invoke(app, ["roster", "validate", "--help"])

    assert result.exit_code == 0
    assert "--mode" in result.stdout
    assert "groups" in result.stdout
    assert "individuals" in result.stdout
    assert "--add-repository" in result.stdout


def test_mutating_command_help_exposes_wait_for_limits(runner) -> None:
    from gh_edu.cli import app

    commands = [
        ["provision", "groups", "--help"],
        ["provision", "individual", "--help"],
        ["provision", "individuals", "--help"],
        ["invitations", "retry-expired", "--help"],
        ["semester", "close", "--help"],
    ]

    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0
        assert "--wait-for-limits" in result.stdout


def test_tty_progress_uses_one_updating_aggregate_line(monkeypatch) -> None:
    from gh_edu.cli import ConsoleProgress

    emitted: list[tuple[str, bool]] = []
    monkeypatch.setattr("gh_edu.cli.sys.stdout", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        "gh_edu.cli.typer.echo",
        lambda message="", *, nl=True, **_kwargs: emitted.append((str(message), nl)),
    )
    reporter = ConsoleProgress()

    reporter.update(
        ExecutionProgress(
            processed=25,
            total=100,
            successful=24,
            failed=1,
            phase="create repository",
            elapsed_seconds=90,
        )
    )

    assert emitted == [
        (
            "\r\x1b[2KApply 25/100 (25%) | create repository | ok 24 failed 1 | "
            "elapsed 1m 30s | minimum ETA 1m 15s",
            False,
        )
    ]


def test_non_tty_progress_emits_every_ten_writes_or_thirty_seconds(
    monkeypatch,
) -> None:
    from gh_edu.cli import ConsoleProgress

    emitted: list[str] = []
    times = iter([1.0, 2.0, 32.0])
    monkeypatch.setattr("gh_edu.cli.sys.stdout", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("gh_edu.cli.time.monotonic", lambda: next(times))
    monkeypatch.setattr(
        "gh_edu.cli.typer.echo",
        lambda message="", **_kwargs: emitted.append(str(message)),
    )
    reporter = ConsoleProgress()

    for processed in (1, 10, 11):
        reporter.update(
            ExecutionProgress(
                processed=processed,
                total=100,
                successful=processed,
                failed=0,
                phase="send invitation",
                elapsed_seconds=processed,
            )
        )

    assert len(emitted) == 2
    assert emitted[0].startswith("Apply 10/100")
    assert emitted[1].startswith("Apply 11/100")
    assert all("@" not in line for line in emitted)


def test_roster_validate_writes_markdown_without_calling_github(
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
            "roster",
            "validate",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
        ],
    )

    assert result.exit_code == 0
    assert "Roster valid" in result.stdout
    assert "Mode: groups" in result.stdout
    assert "Students: 1" in result.stdout
    assert "Project groups: 1" in result.stdout
    assert not fake_client.calls
    reports = list((config_path.parent / "reports").glob("*_roster-validation.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert report.startswith("# GitHub Roster Validation\n")
    assert "- Mode: `groups`" in report
    assert "| Project groups | 1 |" in report
    assert "### Group `G01`" in report


def test_roster_validate_explicit_group_mode_still_requires_group_id(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory(
        [{"student_id": "12345678", "email": "12345678@student.example.edu.au"}],
        headers=["student_id", "email"],
    )

    result = invoke_cli(
        fake_client,
        [
            "roster",
            "validate",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--mode",
            "groups",
        ],
    )

    assert result.exit_code == 2
    assert "missing required column(s): group_id" in result.output
    assert not fake_client.calls


def test_roster_validate_individual_mode_derives_group_id_and_writes_mode_aware_report(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory(
        [{"student_id": "00123456", "email": "00123456@student.example.edu.au"}],
        headers=["student_id", "email"],
    )

    result = invoke_cli(
        fake_client,
        [
            "roster",
            "validate",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--mode",
            "individuals",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Mode: individuals" in result.stdout
    assert "Students: 1" in result.stdout
    assert "Individual teams: 1" in result.stdout
    assert not fake_client.calls
    reports = list((config_path.parent / "reports").glob("*_roster-validation.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert "- Mode: `individuals`" in report
    assert "| Individual teams | 1 |" in report
    assert "| Expected repositories | 0 |" in report
    assert "### Individual `IND-00123456`" in report


def test_roster_validate_individual_repository_mode_validates_repository_assignments(
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
                "repository": "COMP3018-W01-00123456",
            }
        ],
        headers=["student_id", "email", "repository"],
    )

    result = invoke_cli(
        fake_client,
        [
            "roster",
            "validate",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--mode",
            "individuals",
            "--add-repository",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Individual teams: 1" in result.stdout
    assert not fake_client.calls
    reports = list((config_path.parent / "reports").glob("*_roster-validation.md"))
    report = reports[0].read_text(encoding="utf-8")
    assert "| Expected repositories | 1 |" in report
    assert "`COMP3018-W01-00123456`" in report


def test_roster_validate_individual_repository_mode_requires_repository_header(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory(
        [{"student_id": "00123456", "email": "00123456@student.example.edu.au"}],
        headers=["student_id", "email"],
    )

    result = invoke_cli(
        fake_client,
        [
            "roster",
            "validate",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--mode",
            "individuals",
            "--add-repository",
        ],
    )

    assert result.exit_code == 2
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
def test_roster_validate_individual_repository_mode_rejects_bad_assignments(
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
                "repository": repositories[0],
            },
            {
                "student_id": "87654321",
                "email": "87654321@student.example.edu.au",
                "repository": repositories[1],
            },
        ],
        headers=["student_id", "email", "repository"],
    )

    result = invoke_cli(
        fake_client,
        [
            "roster",
            "validate",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--mode",
            "individuals",
            "--add-repository",
        ],
    )

    assert result.exit_code == 2
    assert message in result.output
    assert not fake_client.calls


def test_roster_validate_rejects_repository_option_for_group_mode(
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
            "roster",
            "validate",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--add-repository",
        ],
    )

    assert result.exit_code == 2
    assert "--add-repository is only valid with --mode individuals" in result.output
    assert not fake_client.calls


def test_roster_validate_individual_mode_enforces_configured_github_login_column(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory(overrides={"roster": {"github_login_column": "github_login"}})
    roster_path = roster_factory(
        [{"student_id": "00123456", "email": "00123456@student.example.edu.au"}],
        headers=["student_id", "email"],
    )

    result = invoke_cli(
        fake_client,
        [
            "roster",
            "validate",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
            "--mode",
            "individuals",
        ],
    )

    assert result.exit_code == 2
    assert "missing required column(s): github_login" in result.output
    assert not fake_client.calls
    failed_reports = list((config_path.parent / "reports").glob("*_roster-validation-failed.md"))
    assert len(failed_reports) == 1
    assert "- Mode: `individuals`" in failed_reports[0].read_text(encoding="utf-8")


def test_group_cli_dry_run_has_no_remote_writes_and_prints_brief_summary(
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
        ],
    )

    assert result.exit_code == 0
    assert "Plan complete" in result.stdout
    assert "Create teams: 1" in result.stdout
    assert "Create repositories: 1" in result.stdout
    assert "Send invitations: 1" in result.stdout
    assert not fake_client.write_calls
    assert list((config_path.parent / "reports").glob("*_provision-plan.md"))


def test_apply_plan_report_exists_before_first_github_write(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    config = load_configuration(config_path)
    report_directory = reports_path(config_path, config)
    original_create_team = fake_client.create_team

    def checking_create_team(org: str, name: str):
        assert list(report_directory.glob("*_provision-plan.md"))
        return original_create_team(org, name)

    fake_client.create_team = checking_create_team

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

    assert result.exit_code == 0
    assert "Apply complete" in result.stdout
    assert list(report_directory.glob("*_provision-apply.md"))


def test_apply_reports_aggregate_progress_and_persists_execution_state(
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
    assert "Discovering GitHub state" in result.output
    assert "Apply 1/4" in result.output
    assert "Apply 4/4 (100%)" in result.output
    assert "Verifying completed GitHub changes" in result.output
    assert "12345678@student.example.edu.au" not in result.output
    state_file = config_path.parent / ".gh-edu" / "teaching-org-execution-state.json"
    assert state_file.exists()
    report = next((config_path.parent / "reports").glob("*_provision-apply.md"))
    content = report.read_text(encoding="utf-8")
    assert "## Execution pacing" in content
    assert "One-second pacing wait" in content
    assert "Rate-limit retries: 0" in content


def test_wait_for_limits_retries_server_rate_limit(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    fake_client.fail_next(
        "create_team",
        GitHubRateLimitError("secondary rate limit", retry_after_seconds=120),
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
            "--wait-for-limits",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Paused: GitHub reported a recoverable rate limit" in result.output
    create_calls = [call for call in fake_client.calls if call.operation == "create_team"]
    assert len(create_calls) == 2
    report = next((config_path.parent / "reports").glob("*_provision-apply.md"))
    assert "Rate-limit retries: 1" in report.read_text(encoding="utf-8")


def test_apply_without_wait_stops_at_local_hourly_budget(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory(overrides={"execution": {"content_writes_per_hour": 1}})
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

    assert result.exit_code == 5
    assert "Apply paused by an execution limit" in result.output
    assert "retry at" in result.output
    assert "--wait-for-limits" in result.output
    assert len(fake_client.write_calls) == 1


def test_interrupted_apply_is_resumable_from_remote_and_execution_state(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    fake_client.fail_next("create_team", KeyboardInterrupt())

    interrupted = invoke_cli(
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

    assert interrupted.exit_code == 130
    assert "rerun the same command" in interrupted.output
    state_file = config_path.parent / ".gh-edu" / "teaching-org-execution-state.json"
    assert state_file.exists()

    resumed = invoke_cli(
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

    assert resumed.exit_code == 0, resumed.output
    assert "Apply complete" in resumed.output
    create_team_calls = [
        call for call in fake_client.calls if call.operation == "create_team"
    ]
    assert len(create_team_calls) == 2


def test_auth_check_success_and_authorisation_exit_code(
    config_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    success = invoke_cli(
        fake_client,
        ["auth", "check", "--config", str(config_path)],
    )
    assert success.exit_code == 0
    assert "Authentication valid for course-admin" in success.stdout
    assert "organisation teaching-org is accessible" in success.stdout

    fake_client.clear_calls()
    fake_client.fail_next("check_auth", GitHubAuthError("not logged in"))
    failure = invoke_cli(
        fake_client,
        ["auth", "check", "--config", str(config_path)],
    )
    assert failure.exit_code == 4
    assert "not logged in" in failure.output


def test_validation_failure_exit_code_and_failed_markdown_report(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory(
        [
            {
                "student_id": "12345678",
                "email": "malformed",
                "group_id": "G01",
            }
        ]
    )

    result = invoke_cli(
        fake_client,
        [
            "roster",
            "validate",
            "--config",
            str(config_path),
            "--roster",
            str(roster_path),
        ],
    )

    assert result.exit_code == 2
    assert "email is malformed" in result.output
    assert not fake_client.calls
    failed_reports = list((config_path.parent / "reports").glob("*_roster-validation-failed.md"))
    assert len(failed_reports) == 1
    assert "Validation failed" in failed_reports[0].read_text(encoding="utf-8")


def test_rate_limit_and_unexpected_github_failures_have_distinct_exit_codes(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    command = [
        "status",
        "--config",
        str(config_path),
        "--roster",
        str(roster_path),
    ]

    fake_client.fail_next(
        "check_auth",
        GitHubRateLimitError("rate limit exhausted"),
    )
    rate_limited = invoke_cli(fake_client, command)
    assert rate_limited.exit_code == 5
    assert "rate limit exhausted" in rate_limited.output

    fake_client.clear_calls()
    fake_client.fail_next("check_auth", GitHubError("unexpected adapter failure"))
    unexpected = invoke_cli(fake_client, command)
    assert unexpected.exit_code == 1
    assert "unexpected adapter failure" in unexpected.output


def test_semester_apply_requires_exact_term_confirmation_before_discovery(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
) -> None:
    config_path = config_factory()
    roster_path = roster_factory()
    base = [
        "semester",
        "close",
        "--config",
        str(config_path),
        "--roster",
        str(roster_path),
        "--archive-repositories",
        "--apply",
    ]

    missing = invoke_cli(fake_client, base)
    wrong = invoke_cli(fake_client, [*base, "--confirm-term", "2026S1"])

    assert missing.exit_code == 6
    assert wrong.exit_code == 6
    assert "--confirm-term must exactly match" in missing.output
    assert not fake_client.calls


def test_partial_apply_returns_three_and_still_writes_apply_report(
    config_factory,
    roster_factory,
    fake_client,
    invoke_cli,
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
    fake_client.fail_next(
        "create_repository_from_template",
        GitHubError("repository creation failed"),
        target="COMP3018-2026S2-G01",
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

    assert result.exit_code == 3
    assert "Apply partially complete" in result.stdout
    reports = list((config_path.parent / "reports").glob("*_provision-apply.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert "repository creation failed" in report
    assert "blocked by failed prerequisite" in report


def test_cli_usage_error_is_exit_two(runner) -> None:
    from gh_edu.cli import app

    result = runner.invoke(app, ["provision", "individual"])

    assert result.exit_code == 2
    assert "Missing option" in result.output
