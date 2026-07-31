"""Typer command surface for gh-edu."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from gh_edu import __version__
from gh_edu.core import (
    EXIT_AUTH,
    EXIT_RATE_LIMIT,
    ApplicationError,
    Configuration,
    ConfirmationError,
    DesiredGroup,
    ExecutionOutcome,
    InputValidationError,
    InvitationLedger,
    Plan,
    Roster,
    RosterMode,
    build_group_resources,
    build_individual_resource,
    build_individual_resources,
    build_provision_plan,
    build_semester_close_plan,
    discover_snapshot,
    execute_plan,
    ledger_path,
    load_configuration,
    load_ledger,
    load_roster,
    reports_path,
    terminal_summary_lines,
    verify_execution,
    write_markdown_report,
    write_plan_report,
    write_roster_validation_report,
)
from gh_edu.github import (
    GhCliClient,
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubRateLimitError,
)

app = typer.Typer(
    name="gh-edu",
    help=(
        "Provision GitHub Education resources, invitations, and memberships "
        "from YAML and CSV inputs."
    ),
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
auth_app = typer.Typer(help="Check GitHub CLI authentication.", no_args_is_help=True)
roster_app = typer.Typer(help="Validate LMS roster data.", no_args_is_help=True)
provision_app = typer.Typer(help="Plan or apply provisioning.", no_args_is_help=True)
invitations_app = typer.Typer(
    help="Reconcile controlled invitation retries.",
    no_args_is_help=True,
)
semester_app = typer.Typer(help="Close a teaching semester.", no_args_is_help=True)

app.add_typer(auth_app, name="auth")
app.add_typer(roster_app, name="roster")
app.add_typer(provision_app, name="provision")
app.add_typer(invitations_app, name="invitations")
app.add_typer(semester_app, name="semester")

ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config",
        exists=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="YAML configuration file.",
    ),
]
RosterOption = Annotated[
    Path,
    typer.Option(
        "--roster",
        exists=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="CSV student roster.",
    ),
]
ApplyOption = Annotated[
    bool,
    typer.Option(
        "--apply",
        help="Perform the planned GitHub writes. Without this flag, use dry-run mode.",
    ),
]
AddIndividualOption = Annotated[
    bool,
    typer.Option(
        "--add-individual",
        help=(
            "Also create one team-only individual team per student; new "
            "students receive it in the same organisation invitation."
        ),
    ),
]
AddRepositoryOption = Annotated[
    bool,
    typer.Option(
        "--add-repository",
        help=(
            "Create or reuse each CSV row's exact repository and grant it to "
            "that student's individual team."
        ),
    ),
]
RosterModeOption = Annotated[
    RosterMode,
    typer.Option(
        "--mode",
        case_sensitive=False,
        help="Roster workflow to validate.",
    ),
]
ValidateRepositoryOption = Annotated[
    bool,
    typer.Option(
        "--add-repository",
        help="Require and validate each individual roster row's repository.",
    ),
]


def make_client(_config: Configuration) -> GitHubClient:
    """Construct the production client; tests replace this function."""

    return GhCliClient()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"gh-edu {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed version.",
        ),
    ] = None,
) -> None:
    """Provision GitHub Education resources, invitations, and memberships."""


def _fail(error: Exception) -> NoReturn:
    if isinstance(error, GitHubRateLimitError):
        exit_code = EXIT_RATE_LIMIT
    elif isinstance(error, GitHubAuthError):
        exit_code = EXIT_AUTH
    elif isinstance(error, ApplicationError):
        exit_code = error.exit_code
    elif isinstance(error, GitHubError):
        exit_code = 1
    else:
        exit_code = 1
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=exit_code)


def _print_result(label: str, report: Path, plan: Plan) -> None:
    typer.echo(label)
    typer.echo(f"Report: {report}")
    for line in terminal_summary_lines(plan):
        typer.echo(line)


def _load_group_inputs(
    config_path: Path,
    roster_path: Path,
    *,
    add_individual: bool = False,
) -> tuple[Configuration, Roster, list[DesiredGroup]]:
    config = load_configuration(config_path)
    roster = load_roster(roster_path, config)
    groups = build_group_resources(
        config,
        roster,
        add_individual=add_individual,
    )
    return config, roster, groups


def _apply_and_report(
    *,
    config_path: Path,
    config: Configuration,
    client: GitHubClient,
    plan: Plan,
    ledger_file: Path,
    ledger: InvitationLedger,
    report_kind: str,
    report_title: str,
) -> ExecutionOutcome:
    # The plan report is written by the caller before this function.  That is a
    # hard safety boundary: no remote mutation can occur until the plan exists.
    outcome = execute_plan(
        plan,
        client=client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_file,
    )
    outcome = verify_execution(outcome, client=client, config=config)
    outcome.plan.title = report_title
    apply_report = write_plan_report(
        config_path,
        config,
        outcome.plan,
        kind=report_kind,
    )
    label = "Apply complete" if outcome.exit_code == 0 else "Apply partially complete"
    _print_result(label, apply_report, outcome.plan)
    if outcome.exit_code:
        raise typer.Exit(code=outcome.exit_code)
    return outcome


@auth_app.command("check")
def auth_check(config_path: ConfigOption) -> None:
    """Check active GitHub CLI authentication and organisation ownership."""

    try:
        config = load_configuration(config_path)
        client = make_client(config)
        login = client.check_auth()
        client.check_organisation(config.organisation)
        typer.echo(
            f"Authentication valid for {login}; organisation {config.organisation} is accessible."
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc)


@roster_app.command("validate")
def roster_validate(
    config_path: ConfigOption,
    roster_path: RosterOption,
    mode: RosterModeOption = RosterMode.GROUPS,
    add_repository: ValidateRepositoryOption = False,
) -> None:
    """Validate configuration, roster rows, and generated names."""

    config: Configuration | None = None
    try:
        config = load_configuration(config_path)
        if add_repository and mode == RosterMode.GROUPS:
            raise InputValidationError(
                "--add-repository is only valid with --mode individuals"
            )
        if mode == RosterMode.INDIVIDUALS:
            roster = load_roster(
                roster_path,
                config,
                include_repository=add_repository,
                derive_individual_group_id=True,
            )
            groups = build_individual_resources(
                config,
                roster,
                add_repository=add_repository,
            )
            resource_label = "Individual teams"
        else:
            roster = load_roster(roster_path, config)
            groups = build_group_resources(config, roster)
            resource_label = "Project groups"
        report = write_roster_validation_report(
            config_path,
            config,
            roster,
            groups,
            mode=mode,
        )
        typer.echo("Roster valid")
        typer.echo(f"Report: {report}")
        typer.echo(f"Mode: {mode.value}")
        typer.echo(f"Students: {len(roster.students)}")
        typer.echo(f"{resource_label}: {len(groups)}")
    except typer.Exit:
        raise
    except Exception as exc:
        if config is not None:
            try:
                message = str(exc).replace("\r", " ").replace("\n", "\n- ")
                content = (
                    "# GitHub Roster Validation\n\n"
                    f"- Organisation: `{config.organisation}`\n"
                    f"- Subject: `{config.subject}`\n"
                    f"- Term: `{config.term}`\n"
                    f"- Mode: `{mode.value}`\n\n"
                    "## Result\n\nValidation failed.\n\n"
                    f"- {message}\n"
                )
                report = write_markdown_report(
                    reports_path(config_path, config),
                    kind="roster-validation-failed",
                    content=content,
                )
                typer.echo(f"Report: {report}", err=True)
            except OSError:
                pass
        _fail(exc)


@app.command("status")
def status(config_path: ConfigOption, roster_path: RosterOption) -> None:
    """Discover current resources and invitation states without writing GitHub."""

    try:
        config, _roster, groups = _load_group_inputs(config_path, roster_path)
        ledger_file = ledger_path(config_path, config)
        ledger = load_ledger(ledger_file, config.organisation)
        client = make_client(config)
        snapshot = discover_snapshot(client, config, groups, ledger)
        plan = build_provision_plan(
            config,
            groups,
            snapshot,
            mode="Read only",
            title="GitHub Education Status",
        )
        report = write_plan_report(config_path, config, plan, kind="status")
        _print_result("Status complete", report, plan)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc)


@provision_app.command("groups")
def provision_groups(
    config_path: ConfigOption,
    roster_path: RosterOption,
    add_individual: AddIndividualOption = False,
    apply: ApplyOption = False,
) -> None:
    """Provision each roster group and safely resolve existing student identities."""

    try:
        config, _roster, groups = _load_group_inputs(
            config_path,
            roster_path,
            add_individual=add_individual,
        )
        ledger_file = ledger_path(config_path, config)
        ledger = load_ledger(ledger_file, config.organisation)
        client = make_client(config)
        snapshot = discover_snapshot(client, config, groups, ledger)
        plan = build_provision_plan(
            config,
            groups,
            snapshot,
            mode="Apply" if apply else "Dry run",
        )
        plan_report = write_plan_report(
            config_path,
            config,
            plan,
            kind="provision-plan",
        )
        _print_result("Plan complete", plan_report, plan)
        if not apply:
            return
        _apply_and_report(
            config_path=config_path,
            config=config,
            client=client,
            plan=plan,
            ledger_file=ledger_file,
            ledger=ledger,
            report_kind="provision-apply",
            report_title="GitHub Provisioning Apply Report",
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc)


@provision_app.command("individual")
def provision_individual(
    config_path: ConfigOption,
    student_id: Annotated[str, typer.Option("--student-id", help="University student ID.")],
    email: Annotated[str, typer.Option("--email", help="University email address.")],
    repository: Annotated[
        str,
        typer.Option("--repository", help="Exact individual repository name."),
    ],
    apply: ApplyOption = False,
) -> None:
    """Provision the one-team-per-student exception workflow."""

    try:
        config = load_configuration(config_path)
        group = build_individual_resource(
            config,
            student_id=student_id,
            email=email,
            repository=repository,
        )
        groups = [group]
        ledger_file = ledger_path(config_path, config)
        ledger = load_ledger(ledger_file, config.organisation)
        client = make_client(config)
        snapshot = discover_snapshot(client, config, groups, ledger)
        plan = build_provision_plan(
            config,
            groups,
            snapshot,
            mode="Apply" if apply else "Dry run",
            title="GitHub Individual Provisioning Plan",
        )
        plan_report = write_plan_report(
            config_path,
            config,
            plan,
            kind="individual-plan",
        )
        _print_result("Plan complete", plan_report, plan)
        if not apply:
            return
        _apply_and_report(
            config_path=config_path,
            config=config,
            client=client,
            plan=plan,
            ledger_file=ledger_file,
            ledger=ledger,
            report_kind="individual-apply",
            report_title="GitHub Individual Provisioning Apply Report",
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc)


@provision_app.command("individuals")
def provision_individuals(
    config_path: ConfigOption,
    roster_path: RosterOption,
    add_repository: AddRepositoryOption = False,
    apply: ApplyOption = False,
) -> None:
    """Provision one individual team for every roster student."""

    try:
        config = load_configuration(config_path)
        roster = load_roster(
            roster_path,
            config,
            include_repository=add_repository,
            derive_individual_group_id=True,
        )
        groups = build_individual_resources(
            config,
            roster,
            add_repository=add_repository,
        )
        ledger_file = ledger_path(config_path, config)
        ledger = load_ledger(ledger_file, config.organisation)
        client = make_client(config)
        snapshot = discover_snapshot(
            client,
            config,
            groups,
            ledger,
            require_template=add_repository,
        )
        plan = build_provision_plan(
            config,
            groups,
            snapshot,
            mode="Apply" if apply else "Dry run",
            title="GitHub Batch Individual Provisioning Plan",
        )
        plan_report = write_plan_report(
            config_path,
            config,
            plan,
            kind="individuals-plan",
        )
        _print_result("Plan complete", plan_report, plan)
        if not apply:
            return
        _apply_and_report(
            config_path=config_path,
            config=config,
            client=client,
            plan=plan,
            ledger_file=ledger_file,
            ledger=ledger,
            report_kind="individuals-apply",
            report_title="GitHub Batch Individual Provisioning Apply Report",
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc)


@invitations_app.command("retry-expired")
def retry_expired(
    config_path: ConfigOption,
    roster_path: RosterOption,
    add_individual: AddIndividualOption = False,
    apply: ApplyOption = False,
) -> None:
    """Retry only invitations explicitly confirmed as expired."""

    try:
        config, _roster, groups = _load_group_inputs(
            config_path,
            roster_path,
            add_individual=add_individual,
        )
        ledger_file = ledger_path(config_path, config)
        ledger = load_ledger(ledger_file, config.organisation)
        client = make_client(config)
        snapshot = discover_snapshot(
            client,
            config,
            groups,
            ledger,
            require_template=False,
        )
        plan = build_provision_plan(
            config,
            groups,
            snapshot,
            mode="Apply" if apply else "Dry run",
            retry_expired=True,
            provision_resources=False,
            title="GitHub Expired Invitation Retry Plan",
        )
        plan_report = write_plan_report(
            config_path,
            config,
            plan,
            kind="retry-expired-plan",
        )
        _print_result("Plan complete", plan_report, plan)
        if not apply:
            return
        _apply_and_report(
            config_path=config_path,
            config=config,
            client=client,
            plan=plan,
            ledger_file=ledger_file,
            ledger=ledger,
            report_kind="retry-expired-apply",
            report_title="GitHub Expired Invitation Retry Apply Report",
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc)


@semester_app.command("close")
def semester_close(
    config_path: ConfigOption,
    roster_path: RosterOption,
    archive_repositories: Annotated[
        bool,
        typer.Option(
            "--archive-repositories",
            help="Archive exact cohort repositories.",
        ),
    ] = False,
    remove_team_access: Annotated[
        bool,
        typer.Option(
            "--remove-team-access",
            help="Remove exact team/repository relationships.",
        ),
    ] = False,
    apply: ApplyOption = False,
    confirm_term: Annotated[
        str | None,
        typer.Option(
            "--confirm-term",
            help="Exact configured term required for semester closure.",
        ),
    ] = None,
) -> None:
    """Archive cohort repositories and optionally remove team access."""

    try:
        config, _roster, groups = _load_group_inputs(config_path, roster_path)
        if not archive_repositories and not remove_team_access:
            raise InputValidationError(
                "Semester close requires --archive-repositories, --remove-team-access, or both"
            )
        if confirm_term != config.term:
            raise ConfirmationError(
                f"--confirm-term must exactly match configured term {config.term!r}"
            )
        ledger_file = ledger_path(config_path, config)
        ledger = load_ledger(ledger_file, config.organisation)
        client = make_client(config)
        snapshot = discover_snapshot(
            client,
            config,
            groups,
            ledger,
            require_template=False,
        )
        plan = build_semester_close_plan(
            config,
            groups,
            snapshot,
            archive_repositories=archive_repositories,
            remove_team_access=remove_team_access,
            mode="Apply" if apply else "Dry run",
        )
        plan_report = write_plan_report(
            config_path,
            config,
            plan,
            kind="semester-close-plan",
        )
        _print_result("Plan complete", plan_report, plan)
        if not apply:
            return
        _apply_and_report(
            config_path=config_path,
            config=config,
            client=client,
            plan=plan,
            ledger_file=ledger_file,
            ledger=ledger,
            report_kind="semester-close-apply",
            report_title="GitHub Semester Close Apply Report",
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc)
