"""Typer command surface for gh-edu."""

from __future__ import annotations

import sys
import time
from datetime import datetime
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
    ExecutionPacer,
    ExecutionProgress,
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
    execution_state_path,
    format_duration,
    ledger_path,
    load_configuration,
    load_execution_state,
    load_ledger,
    load_roster,
    lock_execution_state,
    reports_path,
    resolve_invitation_budget,
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
WaitForLimitsOption = Annotated[
    bool,
    typer.Option(
        "--wait-for-limits",
        help="Wait through hourly, daily, and GitHub-provided rate-limit windows.",
    ),
]


class ConsoleProgress:
    """Compact interactive progress with durable non-interactive summaries."""

    def __init__(self) -> None:
        self.interactive = sys.stdout.isatty()
        self.last_reported_at = 0.0
        self.live_line = False

    def _clear_live(self) -> None:
        if self.interactive and self.live_line:
            typer.echo("\r\x1b[2K", nl=False)
            self.live_line = False

    def update(self, progress: ExecutionProgress) -> None:
        percent = round(progress.processed * 100 / progress.total) if progress.total else 100
        remaining = max(0, progress.total - progress.processed)
        line = (
            f"Apply {progress.processed}/{progress.total} ({percent}%) | "
            f"{progress.phase} | ok {progress.successful} failed {progress.failed} | "
            f"elapsed {format_duration(progress.elapsed_seconds)} | "
            f"minimum ETA {format_duration(remaining)}"
        )
        current = time.monotonic()
        if self.interactive:
            typer.echo(f"\r\x1b[2K{line}", nl=False)
            self.live_line = True
        elif (
            progress.processed == progress.total
            or progress.processed % 10 == 0
            or current - self.last_reported_at >= 30
        ):
            typer.echo(line)
            self.last_reported_at = current

    def waiting(self, reason: str, resume_at: datetime, remaining_seconds: int) -> None:
        self._clear_live()
        local_resume = resume_at.astimezone().isoformat(timespec="seconds")
        typer.echo(
            f"Paused: {reason}. Resume at {local_resume} "
            f"({format_duration(remaining_seconds)} remaining)."
        )

    def finish(self) -> None:
        if self.interactive and self.live_line:
            typer.echo()
            self.live_line = False


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
    wait_for_limits: bool,
) -> ExecutionOutcome:
    # The plan report is written by the caller before this function.  That is a
    # hard safety boundary: no remote mutation can occur until the plan exists.
    state_file = execution_state_path(config_path, config)
    reporter = ConsoleProgress()
    total_writes = sum(
        action.is_write and action.status.value == "planned" for action in plan.actions
    )
    invitation_budget = (
        plan.execution_estimate.invitation_budget
        if plan.execution_estimate is not None
        else resolve_invitation_budget(config, None)
    )
    try:
        with lock_execution_state(state_file):
            state = load_execution_state(
                state_file,
                hostname=client.hostname,
                organisation=config.organisation,
            )
            pacer = ExecutionPacer(
                path=state_file,
                state=state,
                content_limit=config.execution.content_writes_per_hour,
                invitation_limit=invitation_budget,
                total_writes=total_writes,
                wait_for_limits=wait_for_limits,
                progress=reporter.update,
                waiting=reporter.waiting,
            )
            outcome = execute_plan(
                plan,
                client=client,
                config=config,
                ledger=ledger,
                ledger_file=ledger_file,
                pacer=pacer,
            )
    except KeyboardInterrupt:
        reporter.finish()
        typer.echo("Apply interrupted; rerun the same command to reconcile and continue.", err=True)
        raise typer.Exit(code=130) from None
    reporter.finish()
    typer.echo("Verifying completed GitHub changes...")
    outcome = verify_execution(outcome, client=client, config=config)
    outcome.plan.title = report_title
    apply_report = write_plan_report(
        config_path,
        config,
        outcome.plan,
        kind=report_kind,
    )
    if outcome.exit_code == 0:
        label = "Apply complete"
    elif outcome.exit_code == EXIT_RATE_LIMIT:
        label = "Apply paused by an execution limit"
    else:
        label = "Apply partially complete"
    _print_result(label, apply_report, outcome.plan)
    if outcome.exit_code:
        metrics = outcome.plan.execution_metrics
        if metrics is not None and metrics.next_eligible_at is not None:
            typer.echo(
                "Execution limit reached; retry at "
                f"{metrics.next_eligible_at.astimezone().isoformat(timespec='seconds')} "
                "or rerun with --wait-for-limits.",
                err=True,
            )
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
            raise InputValidationError("--add-repository is only valid with --mode individuals")
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
        typer.echo("Discovering GitHub state...")
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
    wait_for_limits: WaitForLimitsOption = False,
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
        typer.echo("Discovering GitHub state...")
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
            wait_for_limits=wait_for_limits,
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
    wait_for_limits: WaitForLimitsOption = False,
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
        typer.echo("Discovering GitHub state...")
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
            wait_for_limits=wait_for_limits,
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
    wait_for_limits: WaitForLimitsOption = False,
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
        typer.echo("Discovering GitHub state...")
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
            wait_for_limits=wait_for_limits,
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
    wait_for_limits: WaitForLimitsOption = False,
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
        typer.echo("Discovering GitHub state...")
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
            wait_for_limits=wait_for_limits,
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
    wait_for_limits: WaitForLimitsOption = False,
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
        typer.echo("Discovering GitHub state...")
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
            wait_for_limits=wait_for_limits,
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc)
