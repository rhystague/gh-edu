"""Domain logic for the GitHub Education provisioning CLI.

The planner in this module is deliberately read-only.  All remote writes happen
in :func:`execute_plan`, and only for actions already present in a completed
plan.
"""

from __future__ import annotations

import csv
import fcntl
import json
import math
import os
import re
import string
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from gh_edu.github import (
    FailedInvitation,
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubInvitationLimitError,
    GitHubNetworkError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubResponseError,
    Invitation,
    Organisation,
    Repository,
    Team,
    TeamMember,
)

EXIT_SUCCESS = 0
EXIT_UNEXPECTED = 1
EXIT_VALIDATION = 2
EXIT_PARTIAL = 3
EXIT_AUTH = 4
EXIT_RATE_LIMIT = 5
EXIT_CONFIRMATION = 6


class ApplicationError(Exception):
    """A safe, user-facing application error."""

    exit_code = EXIT_UNEXPECTED


class InputValidationError(ApplicationError):
    """Configuration, roster, naming, or ledger validation failed."""

    exit_code = EXIT_VALIDATION


class ExecutionLimitError(ApplicationError):
    """A local or remote pacing window requires a later retry."""

    exit_code = EXIT_RATE_LIMIT

    def __init__(self, message: str, *, next_eligible_at: datetime) -> None:
        super().__init__(message)
        self.next_eligible_at = next_eligible_at


class ConfirmationError(ApplicationError):
    """A destructive operation was not correctly confirmed."""

    exit_code = EXIT_CONFIRMATION


class Permission(StrEnum):
    PULL = "pull"
    TRIAGE = "triage"
    PUSH = "push"
    MAINTAIN = "maintain"
    ADMIN = "admin"


class RosterMode(StrEnum):
    GROUPS = "groups"
    INDIVIDUALS = "individuals"


class InvitationState(StrEnum):
    NOT_INVITED = "not_invited"
    PENDING = "pending"
    ACCEPTED_CONFIRMED = "accepted_confirmed"
    ACCEPTED_INFERRED = "accepted_inferred"
    EXPIRED = "expired"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


class ActionType(StrEnum):
    CREATE_TEAM = "CREATE_TEAM"
    CREATE_REPOSITORY = "CREATE_REPOSITORY"
    GRANT_TEAM_REPOSITORY = "GRANT_TEAM_REPOSITORY"
    UPDATE_TEAM_REPOSITORY_PERMISSION = "UPDATE_TEAM_REPOSITORY_PERMISSION"
    ADD_TEAM_MEMBER = "ADD_TEAM_MEMBER"
    SEND_INVITATION = "SEND_INVITATION"
    SKIP_PENDING_INVITATION = "SKIP_PENDING_INVITATION"
    SKIP_ACCEPTED = "SKIP_ACCEPTED"
    SKIP_UNCHANGED = "SKIP_UNCHANGED"
    ARCHIVE_REPOSITORY = "ARCHIVE_REPOSITORY"
    REMOVE_TEAM_REPOSITORY = "REMOVE_TEAM_REPOSITORY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    ERROR = "ERROR"


class ActionStatus(StrEnum):
    PLANNED = "planned"
    SKIPPED = "skipped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    REVIEW = "review"


WRITE_ACTIONS = frozenset(
    {
        ActionType.CREATE_TEAM,
        ActionType.CREATE_REPOSITORY,
        ActionType.GRANT_TEAM_REPOSITORY,
        ActionType.UPDATE_TEAM_REPOSITORY_PERMISSION,
        ActionType.ADD_TEAM_MEMBER,
        ActionType.SEND_INVITATION,
        ActionType.ARCHIVE_REPOSITORY,
        ActionType.REMOVE_TEAM_REPOSITORY,
    }
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


_ATOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_LOGIN_RE = _GITHUB_OWNER_RE
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def _validate_atom(value: str, label: str) -> str:
    value = value.strip()
    if not _ATOM_RE.fullmatch(value):
        raise ValueError(
            f"{label} must start with a letter or number and contain only "
            "letters, numbers, '.', '_' or '-'"
        )
    return value


class NamingSettings(StrictModel):
    group_team: str = "{subject}-{term}-{group_id}"
    individual_team: str = "IND-{student_id}"


class GroupRepositorySettings(StrictModel):
    name: str = "{subject}-{term}-{group_id}"
    description: str = "{subject} {term} project repository for {group_id}"


class RepositorySettings(StrictModel):
    permission: Permission = Permission.PUSH
    group: list[GroupRepositorySettings] = Field(
        default_factory=lambda: [GroupRepositorySettings()]
    )
    individual_description: str = "{subject} {term} individual repository for {student_id}"

    @field_validator("group")
    @classmethod
    def require_group_repositories(
        cls, value: list[GroupRepositorySettings]
    ) -> list[GroupRepositorySettings]:
        if not value:
            raise ValueError("at least one group repository must be configured")
        return value


class PathsSettings(StrictModel):
    ledger: str = ".gh-edu/{subject}-{term}-invitations.json"
    reports: str = "reports"
    execution_state: str = ".gh-edu/{organisation}-execution-state.json"


class ExecutionSettings(StrictModel):
    content_writes_per_hour: int = Field(default=450, strict=True, ge=1, le=450)
    invitation_budget_per_24_hours: Literal["auto"] | int = "auto"

    @field_validator("invitation_budget_per_24_hours", mode="before")
    @classmethod
    def validate_invitation_budget(cls, value: Any) -> Literal["auto"] | int:
        if value == "auto":
            return "auto"
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invitation_budget_per_24_hours must be 'auto' or 1 to 500")
        if not 1 <= value <= 500:
            raise ValueError("invitation_budget_per_24_hours must be 'auto' or 1 to 500")
        return int(value)


class RosterSettings(StrictModel):
    github_login_column: str | None = None

    @field_validator("github_login_column")
    @classmethod
    def validate_optional_header(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or any(character in value for character in "\r\n,"):
            raise ValueError("github_login_column must be a valid CSV header")
        if value in {"student_id", "email", "group_id"}:
            raise ValueError("github_login_column must not reuse student_id, email, or group_id")
        return value


_GROUP_FIELDS = frozenset({"subject", "term", "group_id"})
_INDIVIDUAL_FIELDS = frozenset({"subject", "term", "student_id"})
_PATH_FIELDS = frozenset({"organisation", "subject", "term"})


def _template_fields(value: str, label: str) -> set[str]:
    fields: set[str] = set()
    try:
        parsed = string.Formatter().parse(value)
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if not field_name or format_spec or conversion:
                raise ValueError(f"{label} may use only simple '{{field}}' placeholders")
            fields.add(field_name)
    except ValueError as exc:
        if str(exc).startswith(label):
            raise
        raise ValueError(f"{label} is not a valid format template: {exc}") from exc
    return fields


def _validate_template(
    value: str,
    *,
    label: str,
    allowed: frozenset[str],
    required: str | None = None,
) -> None:
    fields = _template_fields(value, label)
    unknown = fields - allowed
    if unknown:
        raise ValueError(
            f"{label} contains unsupported placeholder(s): {', '.join(sorted(unknown))}"
        )
    if required is not None and required not in fields:
        raise ValueError(f"{label} must include '{{{required}}}'")


class Configuration(StrictModel):
    schema_version: Literal[1]
    organisation: str
    subject: str
    term: str
    template: str
    naming: NamingSettings = Field(default_factory=NamingSettings)
    repositories: RepositorySettings = Field(default_factory=RepositorySettings)
    paths: PathsSettings = Field(default_factory=PathsSettings)
    roster: RosterSettings = Field(default_factory=RosterSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)

    @model_validator(mode="before")
    @classmethod
    def require_strict_schema_version(cls, value: object) -> object:
        if isinstance(value, dict):
            schema_version = value.get("schema_version")
            if type(schema_version) is not int or schema_version != 1:
                raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("organisation")
    @classmethod
    def validate_organisation(cls, value: str) -> str:
        if not _GITHUB_OWNER_RE.fullmatch(value):
            raise ValueError("organisation is not a valid GitHub organisation login")
        return value

    @field_validator("subject", "term")
    @classmethod
    def validate_naming_atom(cls, value: str, info: Any) -> str:
        return _validate_atom(value, str(info.field_name))

    @field_validator("template")
    @classmethod
    def validate_template_reference(cls, value: str) -> str:
        if value.count("/") != 1:
            raise ValueError("template must use owner/repository form")
        owner, repository = value.split("/", maxsplit=1)
        if not _GITHUB_OWNER_RE.fullmatch(owner):
            raise ValueError("template owner is invalid")
        validate_repository_name(repository)
        return value

    @model_validator(mode="after")
    def validate_format_templates(self) -> Configuration:
        _validate_template(
            self.naming.group_team,
            label="naming.group_team",
            allowed=_GROUP_FIELDS,
            required="group_id",
        )
        _validate_template(
            self.naming.individual_team,
            label="naming.individual_team",
            allowed=_INDIVIDUAL_FIELDS,
            required="student_id",
        )
        for index, repository in enumerate(self.repositories.group):
            _validate_template(
                repository.name,
                label=f"repositories.group[{index}].name",
                allowed=_GROUP_FIELDS,
                required="group_id",
            )
            _validate_template(
                repository.description,
                label=f"repositories.group[{index}].description",
                allowed=_GROUP_FIELDS,
            )
        _validate_template(
            self.repositories.individual_description,
            label="repositories.individual_description",
            allowed=_INDIVIDUAL_FIELDS,
        )
        _validate_template(
            self.paths.ledger,
            label="paths.ledger",
            allowed=_PATH_FIELDS,
        )
        _validate_template(
            self.paths.reports,
            label="paths.reports",
            allowed=_PATH_FIELDS,
        )
        _validate_template(
            self.paths.execution_state,
            label="paths.execution_state",
            allowed=_PATH_FIELDS,
        )
        return self

    @property
    def template_owner(self) -> str:
        return self.template.split("/", maxsplit=1)[0]

    @property
    def template_repository(self) -> str:
        return self.template.split("/", maxsplit=1)[1]


class Student(StrictModel):
    student_id: str
    email: str
    group_id: str
    github_login: str | None = None
    repository: str | None = None
    row_number: int = 0

    @field_validator("student_id", "group_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _validate_atom(value, str(info.field_name))

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 254 or not _EMAIL_RE.fullmatch(value):
            raise ValueError("email is malformed")
        return value

    @field_validator("github_login")
    @classmethod
    def validate_github_login(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not _LOGIN_RE.fullmatch(value):
            raise ValueError("github_login is malformed")
        return value

    @field_validator("repository")
    @classmethod
    def validate_optional_repository(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("repository is required")
        return validate_repository_name(value)


class Roster(StrictModel):
    students: list[Student]
    source: Path

    @field_validator("students")
    @classmethod
    def require_students(cls, value: list[Student]) -> list[Student]:
        if not value:
            raise ValueError("roster contains no student rows")
        return value


class DesiredRepository(StrictModel):
    name: str
    description: str


class DesiredGroup(StrictModel):
    key: str
    group_id: str
    team_name: str
    repositories: list[DesiredRepository]
    students: list[Student]
    individual: bool = False


class InvitationTarget(StrictModel):
    scope: str
    group_id: str
    team_name: str
    team_slug: str | None = None
    team_id: int | None = Field(default=None, gt=0)
    individual: bool = False


class LedgerRecord(StrictModel):
    student_id: str
    email: str
    group_id: str
    team_name: str
    team_id: int | None = Field(default=None, gt=0)
    invitation_id: int | None = Field(default=None, gt=0)
    invited_at: datetime
    last_seen_pending_at: datetime | None = None
    status: InvitationState
    attempt_count: int = Field(default=1, ge=1)
    github_login: str | None = None
    failure_reason: str | None = None

    @field_validator("student_id", "group_id")
    @classmethod
    def validate_ledger_identifier(cls, value: str, info: Any) -> str:
        return _validate_atom(value, str(info.field_name))

    @field_validator("email")
    @classmethod
    def validate_ledger_email(cls, value: str) -> str:
        return Student.validate_email(value)

    @field_validator("team_name")
    @classmethod
    def validate_ledger_team_name(cls, value: str) -> str:
        return validate_team_name(value)

    @field_validator("github_login")
    @classmethod
    def validate_ledger_login(cls, value: str | None) -> str | None:
        return Student.validate_github_login(value)

    @field_validator("team_id", "invitation_id", "attempt_count", mode="before")
    @classmethod
    def require_strict_positive_integer(cls, value: object, info: Any) -> object:
        if value is None and info.field_name != "attempt_count":
            return value
        if type(value) is not int or value <= 0:
            raise ValueError(f"{info.field_name} must be a positive integer")
        return value

    @model_validator(mode="after")
    def reject_not_invited_record(self) -> LedgerRecord:
        if self.status == InvitationState.NOT_INVITED:
            raise ValueError("ledger records cannot have status not_invited")
        return self

    @field_validator("invited_at", "last_seen_pending_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("ledger timestamps must include a timezone")
        return value

    @field_serializer("invited_at", "last_seen_pending_at")
    def serialise_datetime(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return format_timestamp(value)


class InvitationLedger(StrictModel):
    schema_version: Literal[1] = 1
    organisation: str
    records: list[LedgerRecord] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def require_strict_schema_version(cls, value: object) -> object:
        if isinstance(value, dict):
            schema_version = value.get("schema_version", 1)
            if type(schema_version) is not int or schema_version != 1:
                raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("organisation")
    @classmethod
    def validate_ledger_organisation(cls, value: str) -> str:
        if not _GITHUB_OWNER_RE.fullmatch(value):
            raise ValueError("organisation is not a valid GitHub organisation login")
        return value

    def find(self, email: str, team_name: str) -> LedgerRecord | None:
        email_key = email.casefold()
        team_key = team_name.casefold()
        for record in self.records:
            if record.email.casefold() == email_key and record.team_name.casefold() == team_key:
                return record
        return None

    def find_by_email(self, email: str) -> list[LedgerRecord]:
        email_key = email.casefold()
        return [record for record in self.records if record.email.casefold() == email_key]

    def find_by_student(self, student_id: str) -> list[LedgerRecord]:
        return [record for record in self.records if record.student_id == student_id]


class InvitationDecision(StrictModel):
    state: InvitationState
    action_type: ActionType
    reason: str


class IdentityResolutionState(StrEnum):
    ABSENT = "absent"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class IdentityResolution(StrictModel):
    state: IdentityResolutionState
    team_name: str
    team_slug: str | None = None
    team_id: int | None = Field(default=None, gt=0)
    member: TeamMember | None = None
    reason: str


class Action(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)

    action_id: str
    action_type: ActionType
    scope: str
    student_id: str | None = None
    email: str | None = None
    github_login: str | None = None
    github_user_id: int | None = Field(default=None, gt=0)
    group_id: str | None = None
    team_name: str | None = None
    team_slug: str | None = None
    team_id: int | None = None
    identity_team_name: str | None = None
    identity_team_slug: str | None = None
    identity_team_id: int | None = Field(default=None, gt=0)
    invitation_id: int | None = None
    invitation_created_at: str | None = None
    pending_team_ids: list[int] = Field(default_factory=list)
    invitation_targets: list[InvitationTarget] = Field(default_factory=list)
    repository: str | None = None
    description: str | None = None
    current_state: str | None = None
    desired_state: str | None = None
    invitation_state: InvitationState | None = None
    reason: str
    dependencies: list[str] = Field(default_factory=list)
    destructive: bool = False
    status: ActionStatus = ActionStatus.PLANNED
    error: str | None = None

    @property
    def is_write(self) -> bool:
        return self.action_type in WRITE_ACTIONS


class ExecutionEstimate(StrictModel):
    planned_writes: int
    planned_invitations: int
    minimum_seconds: int
    content_windows: int
    invitation_windows: int
    invitation_budget: int


class ExecutionMetrics(StrictModel):
    pacing_wait_seconds: float = 0.0
    limit_wait_seconds: float = 0.0
    rate_limit_retries: int = 0
    next_eligible_at: datetime | None = None


class Plan(StrictModel):
    title: str
    organisation: str
    subject: str
    term: str
    mode: str
    generated_at: datetime
    actions: list[Action]
    warnings: list[str] = Field(default_factory=list)
    execution_estimate: ExecutionEstimate | None = None
    execution_metrics: ExecutionMetrics | None = None

    @field_serializer("generated_at")
    def serialise_generated_at(self, value: datetime) -> str:
        return format_timestamp(value)

    @property
    def write_actions(self) -> list[Action]:
        return [action for action in self.actions if action.is_write]


class Snapshot(StrictModel):
    teams: list[Team]
    repositories: list[Repository]
    pending_invitations: list[Invitation]
    failed_invitations: list[FailedInvitation]
    team_members: dict[str, list[TeamMember]]
    permissions: dict[str, str | None]
    ledger: InvitationLedger
    template: Repository | None = None
    invitation_team_ids: dict[int, set[int]] = Field(default_factory=dict)
    organisation: Organisation | None = None

    @property
    def teams_by_name(self) -> dict[str, Team]:
        return {team.name: team for team in self.teams}

    @property
    def repositories_by_name(self) -> dict[str, Repository]:
        return {repository.name: repository for repository in self.repositories}

    @property
    def pending_by_email(self) -> dict[str, Invitation]:
        return {
            invitation.email.casefold(): invitation
            for invitation in self.pending_invitations
            if invitation.email
        }


class ExecutionOutcome(StrictModel):
    plan: Plan
    exit_code: int
    successful_writes: int
    failed_writes: int
    blocked_writes: int


class ExecutionState(StrictModel):
    schema_version: Literal[1] = 1
    hostname: str
    organisation: str
    content_writes: list[datetime] = Field(default_factory=list)
    invitations: list[datetime] = Field(default_factory=list)

    @field_validator("content_writes", "invitations")
    @classmethod
    def require_aware_timestamps(cls, values: list[datetime]) -> list[datetime]:
        if any(value.tzinfo is None for value in values):
            raise ValueError("execution-state timestamps must include a timezone")
        return values


@dataclass(frozen=True, slots=True)
class ExecutionProgress:
    processed: int
    total: int
    successful: int
    failed: int
    phase: str
    elapsed_seconds: float


ProgressCallback = Callable[[ExecutionProgress], None]
WaitCallback = Callable[[str, datetime, int], None]


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def format_validation_error(error: ValidationError) -> str:
    messages: list[str] = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "input"
        messages.append(f"{location}: {item['msg']}")
    return "; ".join(messages)


def load_configuration(path: Path) -> Configuration:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputValidationError(f"Could not read configuration {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise InputValidationError(f"Configuration YAML is malformed: {exc}") from exc
    if not isinstance(raw, dict):
        raise InputValidationError("Configuration root must be a YAML mapping")
    try:
        return Configuration.model_validate(raw)
    except ValidationError as exc:
        raise InputValidationError(
            f"Configuration is invalid: {format_validation_error(exc)}"
        ) from exc


def resolve_config_path(config_path: Path, template: str, config: Configuration) -> Path:
    values = {
        "organisation": config.organisation,
        "subject": config.subject,
        "term": config.term,
    }
    rendered = template.format_map(values)
    path = Path(rendered).expanduser()
    if not path.is_absolute():
        path = config_path.resolve().parent / path
    return path


def ledger_path(config_path: Path, config: Configuration) -> Path:
    return resolve_config_path(config_path, config.paths.ledger, config)


def reports_path(config_path: Path, config: Configuration) -> Path:
    return resolve_config_path(config_path, config.paths.reports, config)


def execution_state_path(config_path: Path, config: Configuration) -> Path:
    return resolve_config_path(config_path, config.paths.execution_state, config)


def resolve_invitation_budget(
    config: Configuration,
    organisation: Organisation | None,
    *,
    now: datetime | None = None,
) -> int:
    configured = config.execution.invitation_budget_per_24_hours
    if configured != "auto":
        return configured
    current = now or utc_now()
    old_enough = False
    if organisation is not None:
        try:
            created_at = datetime.fromisoformat(organisation.created_at.replace("Z", "+00:00"))
        except ValueError:
            created_at = None
        if created_at is not None and created_at.tzinfo is not None:
            old_enough = current - created_at.astimezone(UTC) >= timedelta(days=30)
    paid = (
        organisation is not None
        and organisation.plan_name is not None
        and organisation.plan_name.casefold() not in {"free", "free_org"}
    )
    return 450 if old_enough or paid else 45


def attach_execution_estimate(
    plan: Plan,
    config: Configuration,
    organisation: Organisation | None,
) -> None:
    planned = [
        action
        for action in plan.actions
        if action.is_write and action.status == ActionStatus.PLANNED
    ]
    write_count = len(planned)
    invitation_count = sum(action.action_type == ActionType.SEND_INVITATION for action in planned)
    content_limit = config.execution.content_writes_per_hour
    invitation_budget = resolve_invitation_budget(
        config,
        organisation,
        now=plan.generated_at,
    )
    content_windows = math.ceil(write_count / content_limit) if write_count else 0
    invitation_windows = math.ceil(invitation_count / invitation_budget) if invitation_count else 0
    one_second_floor = max(0, write_count - 1)
    content_floor = max(0, content_windows - 1) * 3600 + max(
        0, write_count - max(0, content_windows - 1) * content_limit - 1
    )
    invitation_floor = max(0, invitation_windows - 1) * 86400 + max(
        0, invitation_count - max(0, invitation_windows - 1) * invitation_budget - 1
    )
    plan.execution_estimate = ExecutionEstimate(
        planned_writes=write_count,
        planned_invitations=invitation_count,
        minimum_seconds=max(one_second_floor, content_floor, invitation_floor),
        content_windows=content_windows,
        invitation_windows=invitation_windows,
        invitation_budget=invitation_budget,
    )
    if content_windows > 1:
        plan.warnings.append(
            f"The apply contains {write_count} GitHub writes and spans at least "
            f"{content_windows} hourly pacing windows."
        )
    if invitation_windows > 1:
        plan.warnings.append(
            f"The apply contains {invitation_count} invitations and spans at least "
            f"{invitation_windows} rolling 24-hour invitation windows."
        )


def load_execution_state(
    path: Path,
    *,
    hostname: str,
    organisation: str,
) -> ExecutionState:
    if not path.exists():
        return ExecutionState(hostname=hostname, organisation=organisation)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputValidationError(f"Could not read execution state {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputValidationError(f"Execution state {path} is corrupted: {exc}") from exc
    try:
        state = ExecutionState.model_validate(raw)
    except ValidationError as exc:
        raise InputValidationError(
            f"Execution state {path} is invalid: {format_validation_error(exc)}"
        ) from exc
    if state.hostname.casefold() != hostname.casefold():
        raise InputValidationError(
            f"Execution state {path} belongs to GitHub hostname {state.hostname!r}, "
            f"not {hostname!r}"
        )
    if state.organisation.casefold() != organisation.casefold():
        raise InputValidationError(
            f"Execution state {path} belongs to organisation {state.organisation!r}, "
            f"not {organisation!r}"
        )
    return state


def save_execution_state_atomic(path: Path, state: ExecutionState) -> None:
    content = json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    _write_text_atomic(path, content)


@contextmanager
def lock_execution_state(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    try:
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise InputValidationError(f"Could not open execution lock {lock_path}: {exc}") from exc
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InputValidationError(
                f"Another gh-edu apply is already using execution state {path}"
            ) from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class ExecutionPacer:
    """Persist and enforce conservative rolling GitHub write budgets."""

    def __init__(
        self,
        *,
        path: Path,
        state: ExecutionState,
        content_limit: int,
        invitation_limit: int,
        total_writes: int,
        wait_for_limits: bool,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        progress: ProgressCallback | None = None,
        waiting: WaitCallback | None = None,
    ) -> None:
        self.path = path
        self.state = state
        self.content_limit = content_limit
        self.invitation_limit = invitation_limit
        self.wait_for_limits = wait_for_limits
        self.now = now
        self.sleep = sleep
        self.progress = progress
        self.waiting = waiting
        self.metrics = ExecutionMetrics()
        self.total_writes = total_writes
        self.processed = 0
        self.successful = 0
        self.failed = 0
        self.started_at = now()
        self.last_attempt_finished_at: datetime | None = None
        self._remote_retries = 0

    def _prune(self, current: datetime) -> None:
        content_cutoff = current - timedelta(hours=1)
        invitation_cutoff = current - timedelta(hours=24)
        self.state.content_writes = [
            value for value in self.state.content_writes if value > content_cutoff
        ]
        self.state.invitations = [
            value for value in self.state.invitations if value > invitation_cutoff
        ]

    def _wait_until(self, reason: str, resume_at: datetime, *, pacing: bool) -> None:
        current = self.now()
        remaining = max(0.0, (resume_at - current).total_seconds())
        if remaining <= 0:
            return
        if not pacing and not self.wait_for_limits:
            self.metrics.next_eligible_at = resume_at
            raise ExecutionLimitError(
                f"{reason}; retry at {format_timestamp(resume_at)} or use --wait-for-limits",
                next_eligible_at=resume_at,
            )
        while remaining > 0:
            if not pacing and self.waiting is not None:
                self.waiting(reason, resume_at, math.ceil(remaining))
            step = remaining if pacing else min(60.0, remaining)
            self.sleep(step)
            if pacing:
                self.metrics.pacing_wait_seconds += step
            else:
                self.metrics.limit_wait_seconds += step
            remaining = max(0.0, (resume_at - self.now()).total_seconds())
        self.metrics.next_eligible_at = None

    def before_write(self, *, invitation: bool) -> None:
        current = self.now()
        prior_attempts = self.state.content_writes
        pacing_anchor = self.last_attempt_finished_at
        if prior_attempts:
            persisted_anchor = max(prior_attempts)
            if pacing_anchor is None or persisted_anchor > pacing_anchor:
                pacing_anchor = persisted_anchor
        if pacing_anchor is not None:
            self._wait_until(
                "one-second GitHub mutation delay",
                pacing_anchor + timedelta(seconds=1),
                pacing=True,
            )
            current = self.now()
        self._prune(current)
        waits: list[tuple[str, datetime]] = []
        if len(self.state.content_writes) >= self.content_limit:
            waits.append(
                (
                    "GitHub hourly content-write budget is exhausted",
                    min(self.state.content_writes) + timedelta(hours=1),
                )
            )
        if invitation and len(self.state.invitations) >= self.invitation_limit:
            waits.append(
                (
                    "GitHub rolling 24-hour invitation budget is exhausted",
                    min(self.state.invitations) + timedelta(hours=24),
                )
            )
        if waits:
            reason, resume_at = max(waits, key=lambda item: item[1])
            self.metrics.next_eligible_at = resume_at
            self._wait_until(reason, resume_at, pacing=False)
            current = self.now()
            self._prune(current)
        timestamp = self.now()
        self.state.content_writes.append(timestamp)
        if invitation:
            self.state.invitations.append(timestamp)
        save_execution_state_atomic(self.path, self.state)

    def finish_attempt(self) -> None:
        self.last_attempt_finished_at = self.now()

    def handle_remote_limit(
        self,
        error: GitHubRateLimitError,
        *,
        invitation: bool,
    ) -> None:
        self._remote_retries += 1
        self.metrics.rate_limit_retries += 1
        current = self.now()
        reset_at = (
            datetime.fromtimestamp(error.reset_at_epoch, tz=UTC)
            if error.reset_at_epoch is not None
            else None
        )
        if error.retry_after_seconds is not None:
            resume_at = current + timedelta(seconds=error.retry_after_seconds)
        elif reset_at is not None and reset_at > current:
            resume_at = reset_at
        elif invitation and isinstance(error, GitHubInvitationLimitError):
            future = [
                value + timedelta(hours=24)
                for value in self.state.invitations
                if value + timedelta(hours=24) > current
            ]
            resume_at = min(future) if future else current + timedelta(hours=24)
        else:
            seconds = min(3600, 60 * (2 ** (self._remote_retries - 1)))
            resume_at = current + timedelta(seconds=seconds)
        self.metrics.next_eligible_at = resume_at
        self._wait_until("GitHub reported a recoverable rate limit", resume_at, pacing=False)

    def finish_action(self, *, phase: str, succeeded: bool) -> None:
        self.processed += 1
        if succeeded:
            self.successful += 1
        else:
            self.failed += 1
        if self.progress is not None:
            self.progress(
                ExecutionProgress(
                    processed=self.processed,
                    total=self.total_writes,
                    successful=self.successful,
                    failed=self.failed,
                    phase=phase,
                    elapsed_seconds=max(0.0, (self.now() - self.started_at).total_seconds()),
                )
            )


def validate_repository_name(value: str) -> str:
    value = value.strip()
    if (
        len(value) > 100
        or not _ATOM_RE.fullmatch(value)
        or value in {".", ".."}
        or value.casefold().endswith(".git")
    ):
        raise ValueError(
            "repository names must be 1-100 characters, start with a letter or "
            "number, contain only letters, numbers, '.', '_' or '-', and not end in '.git'"
        )
    return value


def validate_team_name(value: str) -> str:
    value = value.strip()
    if len(value) > 100 or not _ATOM_RE.fullmatch(value):
        raise ValueError(
            "team names must be 1-100 characters, start with a letter or number, "
            "and contain only letters, numbers, '.', '_' or '-'"
        )
    return value


def normalise_resource_name(value: str) -> str:
    normalised = unicodedata.normalize("NFKC", value).strip().casefold()
    normalised = re.sub(r"[^a-z0-9]+", "-", normalised)
    return normalised.strip("-")


def _validate_unique_headers(headers: Sequence[str | None]) -> None:
    clean_headers = [header for header in headers if header is not None]
    duplicates = sorted(header for header, count in Counter(clean_headers).items() if count > 1)
    if duplicates:
        raise InputValidationError(f"Roster contains duplicate header(s): {', '.join(duplicates)}")


def load_roster(
    path: Path,
    config: Configuration,
    *,
    include_repository: bool = False,
    derive_individual_group_id: bool = False,
) -> Roster:
    if include_repository and config.roster.github_login_column == "repository":
        raise InputValidationError(
            "The repository column cannot also be configured as roster.github_login_column"
        )
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise InputValidationError(f"Could not read roster {path}: {exc}") from exc

    students: list[Student] = []
    errors: list[str] = []
    with handle:
        try:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise InputValidationError("Roster has no header row")
            _validate_unique_headers(reader.fieldnames)
            headers = set(reader.fieldnames)
            required = {"student_id", "email"}
            if not derive_individual_group_id:
                required.add("group_id")
            if config.roster.github_login_column is not None:
                required.add(config.roster.github_login_column)
            if include_repository:
                required.add("repository")
            missing = sorted(required - headers)
            if missing:
                raise InputValidationError(
                    f"Roster is missing required column(s): {', '.join(missing)}"
                )

            for row_number, row in enumerate(reader, start=2):
                if None in row:
                    errors.append(f"row {row_number}: too many CSV fields")
                    continue
                if not any((value or "").strip() for value in row.values()):
                    continue
                login_column = config.roster.github_login_column
                student_id = row.get("student_id", "")
                group_id = (
                    row.get("group_id", "")
                    if "group_id" in headers
                    else f"IND-{student_id.strip()}"
                )
                data = {
                    "student_id": student_id,
                    "email": row.get("email", ""),
                    "group_id": group_id,
                    "github_login": row.get(login_column, "") if login_column else None,
                    "repository": row.get("repository", "") if include_repository else None,
                    "row_number": row_number,
                }
                try:
                    students.append(Student.model_validate(data))
                except ValidationError as exc:
                    errors.append(f"row {row_number}: {format_validation_error(exc)}")
        except csv.Error as exc:
            raise InputValidationError(f"Roster CSV is malformed: {exc}") from exc

    seen_ids: dict[str, int] = {}
    seen_emails: dict[str, tuple[str, int]] = {}
    seen_logins: dict[str, tuple[str, int]] = {}
    for student in students:
        if student.student_id in seen_ids:
            errors.append(
                f"row {student.row_number}: duplicate student_id "
                f"{student.student_id!r} (first seen on row {seen_ids[student.student_id]})"
            )
        else:
            seen_ids[student.student_id] = student.row_number
        email_key = student.email.casefold()
        prior_email = seen_emails.get(email_key)
        if prior_email is not None:
            errors.append(
                f"row {student.row_number}: duplicate email {student.email!r} "
                f"(first seen on row {prior_email[1]})"
            )
        else:
            seen_emails[email_key] = (student.student_id, student.row_number)
        if student.github_login is not None:
            login_key = student.github_login.casefold()
            prior_login = seen_logins.get(login_key)
            if prior_login is not None:
                errors.append(
                    f"row {student.row_number}: duplicate github_login "
                    f"{student.github_login!r} (first seen on row {prior_login[1]})"
                )
            else:
                seen_logins[login_key] = (
                    student.student_id,
                    student.row_number,
                )

    if errors:
        raise InputValidationError("Roster is invalid:\n- " + "\n- ".join(errors))
    try:
        return Roster(students=students, source=path)
    except ValidationError as exc:
        raise InputValidationError(f"Roster is invalid: {format_validation_error(exc)}") from exc


def _render(template: str, values: Mapping[str, str]) -> str:
    return template.format_map(values)


def _ensure_desired_names_are_unique(groups: Sequence[DesiredGroup]) -> None:
    team_keys: dict[str, tuple[str, str]] = {}
    repository_keys: dict[str, tuple[str, str]] = {}
    errors: list[str] = []
    for group in groups:
        team_key = normalise_resource_name(group.team_name)
        previous_team = team_keys.get(team_key)
        if previous_team is not None and previous_team[0] != group.team_name:
            errors.append(
                f"team names {previous_team[0]!r} ({previous_team[1]}) and "
                f"{group.team_name!r} ({group.group_id}) normalise to the same name"
            )
        elif previous_team is not None and previous_team[1] != group.group_id:
            errors.append(
                f"groups {previous_team[1]!r} and {group.group_id!r} map to the "
                f"same team {group.team_name!r}"
            )
        else:
            team_keys[team_key] = (group.team_name, group.group_id)

        for repository in group.repositories:
            repository_key = normalise_resource_name(repository.name)
            previous_repository = repository_keys.get(repository_key)
            if previous_repository is not None:
                if previous_repository[0] != repository.name:
                    errors.append(
                        f"repository names {previous_repository[0]!r} "
                        f"({previous_repository[1]}) and {repository.name!r} "
                        f"({group.group_id}) normalise to the same name"
                    )
                elif previous_repository[1] != group.group_id:
                    errors.append(
                        f"groups {previous_repository[1]!r} and {group.group_id!r} "
                        f"map to the same repository {repository.name!r}"
                    )
            else:
                repository_keys[repository_key] = (
                    repository.name,
                    group.group_id,
                )
    if errors:
        raise InputValidationError("Generated resource names collide:\n- " + "\n- ".join(errors))


def build_group_resources(
    config: Configuration,
    roster: Roster,
    *,
    add_individual: bool = False,
) -> list[DesiredGroup]:
    grouped: dict[str, list[Student]] = defaultdict(list)
    for student in roster.students:
        grouped[student.group_id].append(student)

    desired_groups: list[DesiredGroup] = []
    errors: list[str] = []
    for group_id in sorted(grouped):
        values = {
            "subject": config.subject,
            "term": config.term,
            "group_id": group_id,
        }
        team_name = _render(config.naming.group_team, values)
        try:
            validate_team_name(team_name)
        except ValueError as exc:
            errors.append(f"group {group_id}: generated team {team_name!r}: {exc}")
        repositories: list[DesiredRepository] = []
        seen_repository_names: set[str] = set()
        for repository_settings in config.repositories.group:
            name = _render(repository_settings.name, values)
            description = _render(repository_settings.description, values)
            try:
                validate_repository_name(name)
            except ValueError as exc:
                errors.append(f"group {group_id}: generated repository {name!r}: {exc}")
                continue
            repository_key = normalise_resource_name(name)
            if repository_key in seen_repository_names:
                errors.append(f"group {group_id}: duplicate generated repository {name!r}")
                continue
            seen_repository_names.add(repository_key)
            repositories.append(DesiredRepository(name=name, description=description))
        desired_groups.append(
            DesiredGroup(
                key=f"group:{group_id}",
                group_id=group_id,
                team_name=team_name,
                repositories=repositories,
                students=sorted(grouped[group_id], key=lambda item: item.student_id),
            )
        )
    if errors:
        raise InputValidationError(
            "Generated resource names are invalid:\n- " + "\n- ".join(errors)
        )
    identity_groups = build_individual_resources(
        config,
        roster,
        require_group_marker=False,
    )
    _ensure_desired_names_are_unique([*desired_groups, *identity_groups])
    if add_individual:
        desired_groups.extend(identity_groups)
    return desired_groups


def _build_individual_group(
    config: Configuration,
    student: Student,
    *,
    repository: str | None = None,
) -> DesiredGroup:
    individual_group_id = f"IND-{student.student_id}"
    individual_student = Student(
        student_id=student.student_id,
        email=student.email,
        group_id=individual_group_id,
        github_login=student.github_login,
        row_number=student.row_number,
    )
    values = {
        "subject": config.subject,
        "term": config.term,
        "student_id": individual_student.student_id,
    }
    team_name = _render(config.naming.individual_team, values)
    try:
        validate_team_name(team_name)
    except ValueError as exc:
        raise InputValidationError(
            f"Generated individual team {team_name!r} is invalid: {exc}"
        ) from exc
    repositories = (
        [
            DesiredRepository(
                name=validate_repository_name(repository),
                description=_render(
                    config.repositories.individual_description,
                    values,
                ),
            )
        ]
        if repository is not None
        else []
    )
    return DesiredGroup(
        key=f"individual:{individual_student.student_id}",
        group_id=individual_group_id,
        team_name=team_name,
        repositories=repositories,
        students=[individual_student],
        individual=True,
    )


def build_individual_resources(
    config: Configuration,
    roster: Roster,
    *,
    require_group_marker: bool = True,
    add_repository: bool = False,
) -> list[DesiredGroup]:
    """Build individual resources for every roster student."""

    errors = [
        (
            f"row {student.row_number}: group_id must be "
            f"{f'IND-{student.student_id}'!r} for individual provisioning"
        )
        for student in roster.students
        if require_group_marker and student.group_id != f"IND-{student.student_id}"
    ]
    if add_repository:
        errors.extend(
            f"row {student.row_number}: repository is required with --add-repository"
            for student in roster.students
            if student.repository is None
        )
    if errors:
        raise InputValidationError("Individual roster is invalid:\n- " + "\n- ".join(errors))

    groups = [
        _build_individual_group(
            config,
            student,
            repository=student.repository if add_repository else None,
        )
        for student in sorted(roster.students, key=lambda item: item.student_id)
    ]
    _ensure_desired_names_are_unique(groups)
    return groups


def build_individual_resource(
    config: Configuration,
    *,
    student_id: str,
    email: str,
    repository: str,
    github_login: str | None = None,
) -> DesiredGroup:
    try:
        student = Student(
            student_id=student_id,
            email=email,
            group_id=f"IND-{student_id}",
            github_login=github_login,
            row_number=1,
        )
        group = _build_individual_group(
            config,
            student,
            repository=repository,
        )
    except (ValidationError, ValueError, InputValidationError) as exc:
        detail = format_validation_error(exc) if isinstance(exc, ValidationError) else str(exc)
        raise InputValidationError(f"Individual student input is invalid: {detail}") from exc
    _ensure_desired_names_are_unique([group])
    return group


def _identity_groups_for_desired_groups(
    config: Configuration,
    groups: Sequence[DesiredGroup],
) -> list[DesiredGroup]:
    """Return one read-only individual identity target per shared-group student."""

    identities: dict[str, DesiredGroup] = {
        group.students[0].student_id: group
        for group in groups
        if group.individual and len(group.students) == 1
    }
    for group in groups:
        if group.individual:
            continue
        for student in group.students:
            identity = _build_individual_group(config, student)
            existing = identities.get(student.student_id)
            if existing is not None and normalise_resource_name(
                existing.team_name
            ) != normalise_resource_name(identity.team_name):
                raise InputValidationError(
                    f"Student {student.student_id!r} maps to conflicting individual team names"
                )
            identities[student.student_id] = identity
    result = [identities[student_id] for student_id in sorted(identities)]
    _ensure_desired_names_are_unique([*groups, *result])
    return result


def load_ledger(path: Path, organisation: str) -> InvitationLedger:
    if not path.exists():
        return InvitationLedger(organisation=organisation)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputValidationError(f"Could not read invitation ledger {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputValidationError(
            f"Invitation ledger is corrupted at line {exc.lineno}, column {exc.colno}"
        ) from exc
    try:
        ledger = InvitationLedger.model_validate(raw)
    except ValidationError as exc:
        raise InputValidationError(
            f"Invitation ledger is invalid: {format_validation_error(exc)}"
        ) from exc
    if ledger.organisation.casefold() != organisation.casefold():
        raise InputValidationError(
            "Invitation ledger belongs to organisation "
            f"{ledger.organisation!r}, not {organisation!r}"
        )
    keys: set[tuple[str, str]] = set()
    for record in ledger.records:
        key = (record.email.casefold(), record.team_name.casefold())
        if key in keys:
            raise InputValidationError(
                "Invitation ledger contains duplicate records for "
                f"{record.email!r} and team {record.team_name!r}"
            )
        keys.add(key)
    return ledger


def save_ledger_atomic(path: Path, ledger: InvitationLedger) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                ledger.model_dump(mode="json"),
                handle,
                indent=2,
                ensure_ascii=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _replace_or_append_record(
    ledger: InvitationLedger,
    record: LedgerRecord,
) -> None:
    for index, existing in enumerate(ledger.records):
        if (
            existing.email.casefold() == record.email.casefold()
            and existing.team_name.casefold() == record.team_name.casefold()
        ):
            ledger.records[index] = record
            return
    ledger.records.append(record)


def record_successful_invitation(
    ledger: InvitationLedger,
    *,
    student: Student,
    group: DesiredGroup,
    team: Team,
    invitation: Invitation,
    now: datetime,
    attempt_count: int | None = None,
) -> LedgerRecord:
    existing = ledger.find(student.email, group.team_name)
    record = LedgerRecord(
        student_id=student.student_id,
        email=student.email,
        group_id=group.group_id,
        team_name=group.team_name,
        team_id=team.id,
        invitation_id=invitation.id,
        invited_at=_parse_github_datetime(invitation.created_at, now),
        last_seen_pending_at=now,
        status=InvitationState.PENDING,
        attempt_count=(
            attempt_count
            if attempt_count is not None
            else (existing.attempt_count + 1 if existing else 1)
        ),
        github_login=student.github_login,
    )
    _replace_or_append_record(ledger, record)
    return record


def record_failed_invitation(
    ledger: InvitationLedger,
    *,
    student: Student,
    group: DesiredGroup,
    team: Team,
    reason: str,
    now: datetime,
    attempt_count: int | None = None,
) -> LedgerRecord:
    existing = ledger.find(student.email, group.team_name)
    record = LedgerRecord(
        student_id=student.student_id,
        email=student.email,
        group_id=group.group_id,
        team_name=group.team_name,
        team_id=team.id,
        invitation_id=existing.invitation_id if existing else None,
        invited_at=existing.invited_at if existing else now,
        last_seen_pending_at=existing.last_seen_pending_at if existing else None,
        status=InvitationState.FAILED,
        attempt_count=(
            attempt_count
            if attempt_count is not None
            else (existing.attempt_count + 1 if existing else 1)
        ),
        github_login=student.github_login,
        failure_reason=reason,
    )
    _replace_or_append_record(ledger, record)
    return record


def record_observed_pending_invitation(
    ledger: InvitationLedger,
    *,
    action: Action,
    team: Team | None,
    now: datetime,
    target: InvitationTarget | None = None,
    attempt_count: int | None = None,
) -> LedgerRecord:
    selected_target = target
    if selected_target is None and (action.group_id is not None and action.team_name is not None):
        selected_target = InvitationTarget(
            scope=action.scope,
            group_id=action.group_id,
            team_name=action.team_name,
            team_slug=action.team_slug,
            team_id=action.team_id,
            individual=action.scope.startswith("individual:"),
        )
    if (
        action.student_id is None
        or action.email is None
        or selected_target is None
        or action.invitation_id is None
    ):
        raise RuntimeError(f"Pending invitation action {action.action_id} is incomplete")
    expected_team_attached = team is not None and team.id in action.pending_team_ids
    existing = ledger.find(action.email, selected_target.team_name)
    observed_invited_at = _parse_github_datetime(
        action.invitation_created_at,
        now,
    )
    invited_at = (
        existing.invited_at
        if (existing is not None and existing.invitation_id == action.invitation_id)
        else observed_invited_at
    )
    record = LedgerRecord(
        student_id=action.student_id,
        email=action.email,
        group_id=selected_target.group_id,
        team_name=selected_target.team_name,
        team_id=team.id if team is not None else None,
        invitation_id=action.invitation_id,
        invited_at=invited_at,
        last_seen_pending_at=now,
        status=(InvitationState.PENDING if expected_team_attached else InvitationState.FAILED),
        attempt_count=(
            attempt_count
            if attempt_count is not None
            else (
                existing.attempt_count + (existing.invitation_id != action.invitation_id)
                if existing is not None
                else 1
            )
        ),
        github_login=action.github_login,
        failure_reason=(
            None
            if expected_team_attached
            else (
                "Observed pending invitation was not confirmed as attached to "
                "the expected team; review is required if it disappears."
            )
        ),
    )
    _replace_or_append_record(ledger, record)
    return record


def permission_key(team_slug: str, repository: str) -> str:
    return f"{team_slug.casefold()}\0{repository.casefold()}"


def _check_discovered_name_collisions(
    desired_groups: Sequence[DesiredGroup],
    teams: Sequence[Team],
    repositories: Sequence[Repository],
) -> None:
    actual_teams: dict[str, list[str]] = defaultdict(list)
    actual_repositories: dict[str, list[str]] = defaultdict(list)
    for team in teams:
        actual_teams[normalise_resource_name(team.name)].append(team.name)
    for repository in repositories:
        actual_repositories[normalise_resource_name(repository.name)].append(repository.name)

    errors: list[str] = []
    for group in desired_groups:
        for actual_name in actual_teams.get(normalise_resource_name(group.team_name), []):
            if actual_name != group.team_name:
                errors.append(
                    f"desired team {group.team_name!r} collides with existing team {actual_name!r}"
                )
        for desired_repository in group.repositories:
            for actual_name in actual_repositories.get(
                normalise_resource_name(desired_repository.name), []
            ):
                if actual_name != desired_repository.name:
                    errors.append(
                        f"desired repository {desired_repository.name!r} collides "
                        f"with existing repository {actual_name!r}"
                    )
    if errors:
        raise InputValidationError(
            "Existing GitHub resource names collide with desired names:\n- " + "\n- ".join(errors)
        )


def discover_snapshot(
    client: GitHubClient,
    config: Configuration,
    groups: Sequence[DesiredGroup],
    ledger: InvitationLedger,
    *,
    require_template: bool = True,
) -> Snapshot:
    identity_groups = _identity_groups_for_desired_groups(config, groups)
    discovery_groups_by_name = {group.team_name: group for group in [*groups, *identity_groups]}
    discovery_groups = list(discovery_groups_by_name.values())

    client.check_auth()
    client.check_organisation(config.organisation)
    organisation = client.get_organisation(config.organisation)
    template: Repository | None = None
    if require_template:
        try:
            template = client.get_repository(
                config.template_owner,
                config.template_repository,
            )
        except GitHubNotFoundError as exc:
            raise InputValidationError(
                f"Configured template {config.template!r} was not found or is not accessible"
            ) from exc
        if not template.is_template:
            raise InputValidationError(
                f"Configured template {config.template!r} is not marked as a "
                "GitHub template repository"
            )

    teams = client.list_teams(config.organisation)
    repositories = client.list_repositories(config.organisation)
    pending_invitations = client.list_pending_invitations(config.organisation)
    failed_invitations = client.list_failed_invitations(config.organisation)
    _check_discovered_name_collisions(discovery_groups, teams, repositories)
    roster_emails = {student.email.casefold() for group in groups for student in group.students}
    invitation_team_ids: dict[int, set[int]] = {}
    for invitation in pending_invitations:
        if invitation.email is None or invitation.email.casefold() not in roster_emails:
            continue
        try:
            invitation_team_ids[invitation.id] = client.list_invitation_team_ids(
                config.organisation,
                invitation.id,
            )
        except GitHubNotFoundError:
            # The invitation may have been accepted between listing it and
            # reading its team assignments. Treat the assignment as unknown;
            # the ledger adoption path will record it for manual review.
            invitation_team_ids[invitation.id] = set()

    teams_by_name = {team.name: team for team in teams}
    repositories_by_name = {repository.name: repository for repository in repositories}
    team_members: dict[str, list[TeamMember]] = {}
    permissions: dict[str, str | None] = {}
    for group in discovery_groups:
        team = teams_by_name.get(group.team_name)
        if team is None:
            continue
        team_members[team.slug] = client.list_team_members(
            config.organisation,
            team.slug,
        )

    for group in groups:
        team = teams_by_name.get(group.team_name)
        if team is None:
            continue
        for desired_repository in group.repositories:
            repository = repositories_by_name.get(desired_repository.name)
            if repository is None:
                continue
            raw_permission = client.get_team_repository_permission(
                config.organisation,
                team.slug,
                repository.name,
            )
            if raw_permission is None:
                permissions[permission_key(team.slug, repository.name)] = None
                continue
            permission = raw_permission.casefold()
            permission = {"read": "pull", "write": "push"}.get(
                permission,
                permission,
            )
            permissions[permission_key(team.slug, repository.name)] = permission

    return Snapshot(
        teams=teams,
        repositories=repositories,
        pending_invitations=pending_invitations,
        failed_invitations=failed_invitations,
        team_members=team_members,
        permissions=permissions,
        ledger=ledger,
        template=template,
        invitation_team_ids=invitation_team_ids,
        organisation=organisation,
    )


def _matching_failed_invitation(
    record: LedgerRecord | None,
    failed_invitations: Sequence[FailedInvitation],
) -> FailedInvitation | None:
    if record is None or record.invitation_id is None:
        return None
    for invitation in failed_invitations:
        if invitation.id == record.invitation_id:
            return invitation
    return None


def _failure_is_expiry(invitation: FailedInvitation) -> bool:
    reason = (invitation.failed_reason or "").casefold()
    return "expir" in reason


def _members_for_team(snapshot: Snapshot, team: Team | None) -> list[TeamMember]:
    if team is None:
        return []
    return snapshot.team_members.get(team.slug, [])


def _member_logins(members: Sequence[TeamMember]) -> set[str]:
    return {member.login.casefold() for member in members}


def _direct_ordinary_members(members: Sequence[TeamMember]) -> list[TeamMember]:
    return [member for member in members if member.role == "member" and not member.inherited]


def _resolve_student_identity(
    config: Configuration,
    student: Student,
    snapshot: Snapshot,
) -> IdentityResolution:
    identity_group = _build_individual_group(config, student)
    identity_team = snapshot.teams_by_name.get(identity_group.team_name)
    identity_records = [
        record
        for record in snapshot.ledger.records
        if (
            record.student_id == student.student_id
            and (
                record.group_id == identity_group.group_id
                or record.team_name.casefold() == identity_group.team_name.casefold()
            )
        )
        or record.team_name.casefold() == identity_group.team_name.casefold()
    ]

    for record in identity_records:
        if (
            record.student_id != student.student_id
            or record.email.casefold() != student.email.casefold()
            or record.group_id != identity_group.group_id
            or record.team_name.casefold() != identity_group.team_name.casefold()
            or (
                identity_team is not None
                and record.team_id is not None
                and record.team_id != identity_team.id
            )
        ):
            return IdentityResolution(
                state=IdentityResolutionState.UNRESOLVED,
                team_name=identity_group.team_name,
                team_slug=identity_team.slug if identity_team else None,
                team_id=identity_team.id if identity_team else None,
                reason=(
                    "the individual-team ledger identity or numeric team ID "
                    "conflicts with the roster or current GitHub team"
                ),
            )

    if identity_team is None:
        if identity_records:
            return IdentityResolution(
                state=IdentityResolutionState.UNRESOLVED,
                team_name=identity_group.team_name,
                reason=(
                    "the ledger records an individual-team assignment, but the "
                    "expected individual team is missing"
                ),
            )
        return IdentityResolution(
            state=IdentityResolutionState.ABSENT,
            team_name=identity_group.team_name,
            reason="no individual team or individual-team ledger history exists",
        )

    candidates = _direct_ordinary_members(_members_for_team(snapshot, identity_team))
    if not candidates:
        return IdentityResolution(
            state=IdentityResolutionState.UNRESOLVED,
            team_name=identity_group.team_name,
            team_slug=identity_team.slug,
            team_id=identity_team.id,
            reason=(
                "the individual team exists but contains no direct active non-maintainer member"
            ),
        )
    if len(candidates) > 1:
        return IdentityResolution(
            state=IdentityResolutionState.UNRESOLVED,
            team_name=identity_group.team_name,
            team_slug=identity_team.slug,
            team_id=identity_team.id,
            reason=("the individual team contains multiple direct active non-maintainer members"),
        )

    member = candidates[0]
    if (
        student.github_login is not None
        and student.github_login.casefold() != member.login.casefold()
    ):
        return IdentityResolution(
            state=IdentityResolutionState.UNRESOLVED,
            team_name=identity_group.team_name,
            team_slug=identity_team.slug,
            team_id=identity_team.id,
            reason=(
                "the individual team's sole member does not match the verified "
                "GitHub login in the roster"
            ),
        )
    return IdentityResolution(
        state=IdentityResolutionState.RESOLVED,
        team_name=identity_group.team_name,
        team_slug=identity_team.slug,
        team_id=identity_team.id,
        member=member,
        reason=(
            "the individual team's sole direct active non-maintainer member "
            "resolved the student's GitHub identity"
        ),
    )


def reconcile_invitation(
    student: Student,
    group: DesiredGroup,
    snapshot: Snapshot,
) -> InvitationDecision:
    pending = snapshot.pending_by_email.get(student.email.casefold())
    if pending is not None:
        expected_team = snapshot.teams_by_name.get(group.team_name)
        pending_team_ids = snapshot.invitation_team_ids.get(pending.id, set())
        if expected_team is None or expected_team.id not in pending_team_ids:
            return InvitationDecision(
                state=InvitationState.PENDING,
                action_type=ActionType.REVIEW_REQUIRED,
                reason=(
                    "a pending organisation invitation already exists, but the "
                    "expected team is not attached; the invitation will not be "
                    "replaced or duplicated"
                ),
            )
        return InvitationDecision(
            state=InvitationState.PENDING,
            action_type=ActionType.SKIP_PENDING_INVITATION,
            reason=(
                "a pending organisation invitation already exists for this "
                "email and contains the expected team assignment"
            ),
        )

    team = snapshot.teams_by_name.get(group.team_name)
    members = _members_for_team(snapshot, team)
    member_logins = _member_logins(members)
    if student.github_login and student.github_login.casefold() in member_logins:
        return InvitationDecision(
            state=InvitationState.ACCEPTED_CONFIRMED,
            action_type=ActionType.SKIP_ACCEPTED,
            reason="the verified GitHub login is an active member of the expected team",
        )
    individual_members = _direct_ordinary_members(members)
    if group.individual and len(individual_members) == 1:
        return InvitationDecision(
            state=InvitationState.ACCEPTED_CONFIRMED,
            action_type=ActionType.SKIP_ACCEPTED,
            reason="the individual team contains one active non-maintainer member",
        )
    if group.individual and len(individual_members) > 1:
        return InvitationDecision(
            state=InvitationState.UNRESOLVED,
            action_type=ActionType.REVIEW_REQUIRED,
            reason=(
                "the individual team contains multiple active non-maintainer "
                "members, so identity cannot be confirmed"
            ),
        )

    record = snapshot.ledger.find(student.email, group.team_name)
    if record is not None and (
        record.student_id != student.student_id or record.group_id != group.group_id
    ):
        return InvitationDecision(
            state=InvitationState.UNRESOLVED,
            action_type=ActionType.REVIEW_REQUIRED,
            reason=(
                "the roster identity or group no longer matches the prior ledger "
                "record; the mapping must be reviewed"
            ),
        )
    if (
        record is not None
        and record.team_id is not None
        and team is not None
        and record.team_id != team.id
    ):
        return InvitationDecision(
            state=InvitationState.UNRESOLVED,
            action_type=ActionType.REVIEW_REQUIRED,
            reason=(
                "the expected team now has a different numeric ID from the ledger; "
                "the resource mapping must be reviewed"
            ),
        )
    if record is not None and record.status in {
        InvitationState.ACCEPTED_CONFIRMED,
        InvitationState.ACCEPTED_INFERRED,
    }:
        action_type = ActionType.SKIP_ACCEPTED
        return InvitationDecision(
            state=record.status,
            action_type=action_type,
            reason=f"the ledger records the invitation as {record.status.value}",
        )
    if record is not None and record.status == InvitationState.FAILED:
        return InvitationDecision(
            state=InvitationState.FAILED,
            action_type=ActionType.REVIEW_REQUIRED,
            reason="a prior controlled invitation attempt is recorded as failed",
        )

    failed_invitation = _matching_failed_invitation(
        record,
        snapshot.failed_invitations,
    )
    if failed_invitation is not None:
        if _failure_is_expiry(failed_invitation):
            return InvitationDecision(
                state=InvitationState.EXPIRED,
                action_type=ActionType.REVIEW_REQUIRED,
                reason="GitHub explicitly reports that the prior invitation expired",
            )
        return InvitationDecision(
            state=InvitationState.FAILED,
            action_type=ActionType.REVIEW_REQUIRED,
            reason=(
                "GitHub reports that a prior invitation failed"
                + (
                    f": {failed_invitation.failed_reason}"
                    if failed_invitation.failed_reason
                    else ""
                )
            ),
        )

    if record is not None and record.status == InvitationState.EXPIRED:
        return InvitationDecision(
            state=InvitationState.EXPIRED,
            action_type=ActionType.REVIEW_REQUIRED,
            reason="the ledger explicitly records a confirmed expired invitation",
        )
    if record is not None and not group.individual and members:
        return InvitationDecision(
            state=InvitationState.ACCEPTED_INFERRED,
            action_type=ActionType.SKIP_ACCEPTED,
            reason=(
                "the shared team has active members, but this email cannot be "
                "mapped to an individual GitHub account; acceptance is inferred"
            ),
        )
    if record is not None:
        return InvitationDecision(
            state=InvitationState.UNRESOLVED,
            action_type=ActionType.REVIEW_REQUIRED,
            reason=(
                "the prior invitation is no longer pending and acceptance cannot "
                "be conclusively mapped"
            ),
        )

    other_records = snapshot.ledger.find_by_email(student.email)
    if other_records:
        return InvitationDecision(
            state=InvitationState.UNRESOLVED,
            action_type=ActionType.REVIEW_REQUIRED,
            reason=(
                "the ledger contains this email for a different team; the mapping "
                "must be reviewed before another invitation is sent"
            ),
        )
    student_records = snapshot.ledger.find_by_student(student.student_id)
    if student_records:
        return InvitationDecision(
            state=InvitationState.UNRESOLVED,
            action_type=ActionType.REVIEW_REQUIRED,
            reason=(
                "the ledger already contains this student ID with a different "
                "email or team; the identity mapping must be reviewed"
            ),
        )
    return InvitationDecision(
        state=InvitationState.NOT_INVITED,
        action_type=ActionType.SEND_INVITATION,
        reason="no pending invitation or prior ledger record exists",
    )


def reconcile_invitation_bundle(
    student: Student,
    groups: Sequence[DesiredGroup],
    snapshot: Snapshot,
) -> InvitationDecision:
    """Reconcile one student's complete, ordered team-assignment bundle."""

    if not groups:
        raise InputValidationError(f"Student {student.student_id!r} has no invitation targets")
    if len(groups) == 1:
        return reconcile_invitation(student, groups[0], snapshot)

    teams_by_name = snapshot.teams_by_name
    pending = snapshot.pending_by_email.get(student.email.casefold())
    if pending is not None:
        pending_team_ids = snapshot.invitation_team_ids.get(pending.id, set())
        missing_targets = [
            group.team_name
            for group in groups
            if (
                (team := teams_by_name.get(group.team_name)) is None
                or team.id not in pending_team_ids
            )
        ]
        if missing_targets:
            return InvitationDecision(
                state=InvitationState.PENDING,
                action_type=ActionType.REVIEW_REQUIRED,
                reason=(
                    "a pending organisation invitation already exists, but it is "
                    "missing expected team assignment(s): "
                    + ", ".join(missing_targets)
                    + "; the invitation will not be replaced or duplicated"
                ),
            )
        return InvitationDecision(
            state=InvitationState.PENDING,
            action_type=ActionType.SKIP_PENDING_INVITATION,
            reason=(
                "a pending organisation invitation already contains every expected team assignment"
            ),
        )

    expected_names = {group.team_name.casefold() for group in groups}
    email_records = snapshot.ledger.find_by_email(student.email)
    student_records = snapshot.ledger.find_by_student(student.student_id)
    for record in [*email_records, *student_records]:
        if (
            record.email.casefold() != student.email.casefold()
            or record.student_id != student.student_id
            or record.team_name.casefold() not in expected_names
        ):
            return InvitationDecision(
                state=InvitationState.UNRESOLVED,
                action_type=ActionType.REVIEW_REQUIRED,
                reason=(
                    "the ledger contains a conflicting identity or team assignment "
                    "for this student; the bundle must be reviewed"
                ),
            )

    records: list[LedgerRecord | None] = []
    for group in groups:
        target_record = snapshot.ledger.find(student.email, group.team_name)
        team = teams_by_name.get(group.team_name)
        if target_record is not None and (
            target_record.group_id != group.group_id
            or (
                target_record.team_id is not None
                and team is not None
                and target_record.team_id != team.id
            )
        ):
            return InvitationDecision(
                state=InvitationState.UNRESOLVED,
                action_type=ActionType.REVIEW_REQUIRED,
                reason=(
                    "an expected team has a conflicting group or numeric ID in "
                    "the ledger; the bundle must be reviewed"
                ),
            )
        records.append(target_record)

    if student.github_login:
        login = student.github_login.casefold()
        memberships = [
            (team is not None and login in _member_logins(_members_for_team(snapshot, team)))
            for group in groups
            for team in [teams_by_name.get(group.team_name)]
        ]
        if all(memberships):
            return InvitationDecision(
                state=InvitationState.ACCEPTED_CONFIRMED,
                action_type=ActionType.SKIP_ACCEPTED,
                reason=("the verified GitHub login is an active member of every expected team"),
            )
        if any(memberships):
            return InvitationDecision(
                state=InvitationState.UNRESOLVED,
                action_type=ActionType.REVIEW_REQUIRED,
                reason=(
                    "the verified GitHub login belongs to only part of the expected team bundle"
                ),
            )
    else:
        individual_member_sets: list[list[TeamMember]] = []
        for group in groups:
            if not group.individual:
                continue
            team = teams_by_name.get(group.team_name)
            members = _direct_ordinary_members(_members_for_team(snapshot, team))
            if len(members) > 1:
                return InvitationDecision(
                    state=InvitationState.UNRESOLVED,
                    action_type=ActionType.REVIEW_REQUIRED,
                    reason=(
                        "an individual team contains multiple active "
                        "non-maintainer members, so identity cannot be confirmed"
                    ),
                )
            individual_member_sets.append(members)
        inferred_logins = {
            members[0].login.casefold() for members in individual_member_sets if len(members) == 1
        }
        if len(inferred_logins) > 1:
            return InvitationDecision(
                state=InvitationState.UNRESOLVED,
                action_type=ActionType.REVIEW_REQUIRED,
                reason="individual teams imply conflicting GitHub identities",
            )
        if inferred_logins:
            inferred_login = next(iter(inferred_logins))
            memberships = [
                (
                    team is not None
                    and inferred_login in _member_logins(_members_for_team(snapshot, team))
                )
                for group in groups
                for team in [teams_by_name.get(group.team_name)]
            ]
            if all(memberships):
                return InvitationDecision(
                    state=InvitationState.ACCEPTED_CONFIRMED,
                    action_type=ActionType.SKIP_ACCEPTED,
                    reason=(
                        "the individual team's sole member is also active in "
                        "every expected shared team"
                    ),
                )
            return InvitationDecision(
                state=InvitationState.UNRESOLVED,
                action_type=ActionType.REVIEW_REQUIRED,
                reason=(
                    "the individual team's sole member belongs to only part of "
                    "the expected team bundle"
                ),
            )

    if all(record is None for record in records):
        return InvitationDecision(
            state=InvitationState.NOT_INVITED,
            action_type=ActionType.SEND_INVITATION,
            reason="no pending invitation or prior ledger record exists for the bundle",
        )
    if any(record is None for record in records):
        return InvitationDecision(
            state=InvitationState.UNRESOLVED,
            action_type=ActionType.REVIEW_REQUIRED,
            reason=(
                "the ledger contains only part of the expected team bundle; "
                "another invitation will not be sent"
            ),
        )

    decisions = [reconcile_invitation(student, group, snapshot) for group in groups]
    if all(decision.state == InvitationState.ACCEPTED_CONFIRMED for decision in decisions):
        return InvitationDecision(
            state=InvitationState.ACCEPTED_CONFIRMED,
            action_type=ActionType.SKIP_ACCEPTED,
            reason="the ledger confirms acceptance for every expected team",
        )
    if all(decision.state == InvitationState.EXPIRED for decision in decisions):
        invitation_ids = {
            record.invitation_id
            for record in records
            if record is not None and record.invitation_id is not None
        }
        if len(invitation_ids) == 1 and all(
            record is not None and record.invitation_id is not None for record in records
        ):
            return InvitationDecision(
                state=InvitationState.EXPIRED,
                action_type=ActionType.REVIEW_REQUIRED,
                reason=(
                    "GitHub or the ledger explicitly reports the complete "
                    "invitation bundle as expired"
                ),
            )
    if any(decision.state == InvitationState.FAILED for decision in decisions):
        return InvitationDecision(
            state=InvitationState.FAILED,
            action_type=ActionType.REVIEW_REQUIRED,
            reason="at least one prior bundle invitation assignment is recorded as failed",
        )
    return InvitationDecision(
        state=InvitationState.UNRESOLVED,
        action_type=ActionType.REVIEW_REQUIRED,
        reason=(
            "the prior invitation bundle is no longer pending and complete "
            "acceptance cannot be confirmed"
        ),
    )


def _team_action_id(group: DesiredGroup) -> str:
    return f"{group.key}:team"


def _repository_action_id(group: DesiredGroup, repository: str) -> str:
    return f"{group.key}:repository:{repository.casefold()}"


def _permission_action_id(group: DesiredGroup, repository: str) -> str:
    return f"{group.key}:permission:{repository.casefold()}"


def _invitation_action_id(group: DesiredGroup, student: Student) -> str:
    return f"{group.key}:invitation:{student.student_id}"


def _membership_action_id(group: DesiredGroup, student: Student) -> str:
    return f"{group.key}:membership:{student.student_id}"


def _pending_contains_expected_bundle(
    student: Student,
    groups: Sequence[DesiredGroup],
    snapshot: Snapshot,
) -> bool:
    pending = snapshot.pending_by_email.get(student.email.casefold())
    if pending is None:
        return False
    attached_ids = snapshot.invitation_team_ids.get(pending.id, set())
    return all(
        (team := snapshot.teams_by_name.get(group.team_name)) is not None
        and team.id in attached_ids
        for group in groups
    )


def _membership_mapping_conflict(
    student: Student,
    group: DesiredGroup,
    team: Team | None,
    snapshot: Snapshot,
) -> str | None:
    matching_team_records = [
        record
        for record in snapshot.ledger.records
        if record.team_name.casefold() == group.team_name.casefold()
    ]
    for record in matching_team_records:
        if (
            record.student_id != student.student_id
            or record.email.casefold() != student.email.casefold()
            or record.group_id != group.group_id
            or (record.team_id is not None and team is not None and record.team_id != team.id)
        ):
            return (
                "the shared-team ledger identity or numeric team ID conflicts "
                "with the roster or current GitHub team"
            )
    return None


def _record_is_part_of_multi_team_invitation(
    student: Student,
    group: DesiredGroup,
    snapshot: Snapshot,
) -> bool:
    record = snapshot.ledger.find(student.email, group.team_name)
    if record is None or record.invitation_id is None:
        return False
    return (
        sum(
            candidate.invitation_id == record.invitation_id
            for candidate in snapshot.ledger.find_by_email(student.email)
        )
        > 1
    )


def _skipped_action(
    *,
    action_id: str,
    scope: str,
    group: DesiredGroup,
    reason: str,
    repository: str | None = None,
    team: Team | None = None,
    current_state: str | None = None,
    desired_state: str | None = None,
) -> Action:
    return Action(
        action_id=action_id,
        action_type=ActionType.SKIP_UNCHANGED,
        scope=scope,
        group_id=group.group_id,
        team_name=group.team_name,
        team_slug=team.slug if team else None,
        team_id=team.id if team else None,
        repository=repository,
        current_state=current_state,
        desired_state=desired_state,
        reason=reason,
        status=ActionStatus.SKIPPED,
    )


def build_provision_plan(
    config: Configuration,
    groups: Sequence[DesiredGroup],
    snapshot: Snapshot,
    *,
    mode: str,
    generated_at: datetime | None = None,
    retry_expired: bool = False,
    provision_resources: bool = True,
    title: str = "GitHub Provisioning Plan",
) -> Plan:
    now = generated_at or utc_now()
    teams_by_name = snapshot.teams_by_name
    repositories_by_name = snapshot.repositories_by_name
    team_actions: list[Action] = []
    repository_actions: list[Action] = []
    permission_actions: list[Action] = []
    student_actions: list[Action] = []
    group_dependencies: dict[str, list[str]] = {}

    for group in groups:
        team = teams_by_name.get(group.team_name)
        team_action_id = _team_action_id(group)
        if team is None and provision_resources:
            team_actions.append(
                Action(
                    action_id=team_action_id,
                    action_type=ActionType.CREATE_TEAM,
                    scope=group.key,
                    group_id=group.group_id,
                    team_name=group.team_name,
                    desired_state="closed team exists",
                    reason="the expected team does not exist",
                )
            )
        elif team is None:
            team_actions.append(
                Action(
                    action_id=team_action_id,
                    action_type=ActionType.ERROR,
                    scope=group.key,
                    group_id=group.group_id,
                    team_name=group.team_name,
                    desired_state="existing team with a numeric ID",
                    reason="the expected team is missing; run provisioning first",
                    status=ActionStatus.FAILED,
                )
            )
        else:
            team_actions.append(
                _skipped_action(
                    action_id=team_action_id,
                    scope=group.key,
                    group=group,
                    team=team,
                    current_state=f"team {team.slug} (ID {team.id}) exists",
                    desired_state="team exists",
                    reason="the exact expected team already exists",
                )
            )

        group_permission_ids: list[str] = []
        for desired_repository in group.repositories:
            repository = repositories_by_name.get(desired_repository.name)
            repository_action_id = _repository_action_id(group, desired_repository.name)
            permission_action_id = _permission_action_id(group, desired_repository.name)
            group_permission_ids.append(permission_action_id)
            if repository is None and provision_resources:
                repository_actions.append(
                    Action(
                        action_id=repository_action_id,
                        action_type=ActionType.CREATE_REPOSITORY,
                        scope=group.key,
                        group_id=group.group_id,
                        team_name=group.team_name,
                        repository=desired_repository.name,
                        description=desired_repository.description,
                        desired_state="private repository generated from template",
                        reason="the expected repository does not exist",
                    )
                )
            elif repository is None:
                repository_actions.append(
                    Action(
                        action_id=repository_action_id,
                        action_type=ActionType.ERROR,
                        scope=group.key,
                        group_id=group.group_id,
                        team_name=group.team_name,
                        repository=desired_repository.name,
                        desired_state="existing repository",
                        reason=("the expected repository is missing; run provisioning first"),
                        status=ActionStatus.FAILED,
                    )
                )
            elif repository.is_archived:
                repository_actions.append(
                    Action(
                        action_id=repository_action_id,
                        action_type=ActionType.ERROR,
                        scope=group.key,
                        group_id=group.group_id,
                        team_name=group.team_name,
                        repository=repository.name,
                        current_state="repository is archived",
                        desired_state="active repository",
                        reason=(
                            "the existing repository is archived and will not be "
                            "unarchived automatically"
                        ),
                        status=ActionStatus.FAILED,
                    )
                )
            elif not repository.is_private:
                repository_actions.append(
                    Action(
                        action_id=repository_action_id,
                        action_type=ActionType.ERROR,
                        scope=group.key,
                        group_id=group.group_id,
                        team_name=group.team_name,
                        repository=repository.name,
                        current_state="repository is public",
                        desired_state="private repository",
                        reason=(
                            "the existing repository is public and visibility "
                            "will not be changed automatically"
                        ),
                        status=ActionStatus.FAILED,
                    )
                )
            else:
                repository_actions.append(
                    _skipped_action(
                        action_id=repository_action_id,
                        scope=group.key,
                        group=group,
                        repository=repository.name,
                        current_state="active repository exists",
                        desired_state="repository exists",
                        reason="the exact expected repository already exists",
                    )
                )

            dependencies = [team_action_id, repository_action_id]
            if (
                team is not None
                and repository is not None
                and not repository.is_archived
                and repository.is_private
            ):
                current_permission = snapshot.permissions.get(
                    permission_key(team.slug, repository.name)
                )
            else:
                current_permission = None
            if (
                team is not None
                and repository is not None
                and not repository.is_archived
                and repository.is_private
                and current_permission == config.repositories.permission.value
            ):
                permission_actions.append(
                    _skipped_action(
                        action_id=permission_action_id,
                        scope=group.key,
                        group=group,
                        team=team,
                        repository=repository.name,
                        current_state=current_permission,
                        desired_state=config.repositories.permission.value,
                        reason="the team already has the configured repository permission",
                    )
                )
            elif not provision_resources:
                permission_actions.append(
                    Action(
                        action_id=permission_action_id,
                        action_type=ActionType.ERROR,
                        scope=group.key,
                        group_id=group.group_id,
                        team_name=group.team_name,
                        team_slug=team.slug if team else None,
                        team_id=team.id if team else None,
                        repository=desired_repository.name,
                        current_state=(
                            current_permission
                            if current_permission is not None
                            else "no team relationship"
                        ),
                        desired_state=config.repositories.permission.value,
                        reason=(
                            "repository access is not ready; run provisioning "
                            "before retrying invitations"
                        ),
                        dependencies=dependencies,
                        status=ActionStatus.FAILED,
                    )
                )
            else:
                permission_action_type = (
                    ActionType.UPDATE_TEAM_REPOSITORY_PERMISSION
                    if current_permission is not None
                    else ActionType.GRANT_TEAM_REPOSITORY
                )
                permission_actions.append(
                    Action(
                        action_id=permission_action_id,
                        action_type=permission_action_type,
                        scope=group.key,
                        group_id=group.group_id,
                        team_name=group.team_name,
                        team_slug=team.slug if team else None,
                        team_id=team.id if team else None,
                        repository=desired_repository.name,
                        current_state=(
                            current_permission
                            if current_permission is not None
                            else "no team relationship"
                        ),
                        desired_state=config.repositories.permission.value,
                        reason=(
                            "the repository permission differs from the configured value"
                            if current_permission is not None
                            else "the team does not have access to the repository"
                        ),
                        dependencies=dependencies,
                    )
                )

        group_dependencies[group.key] = [team_action_id, *group_permission_ids]

    student_bundles: dict[str, tuple[Student, list[DesiredGroup]]] = {}
    for group in groups:
        for student in group.students:
            existing = student_bundles.get(student.student_id)
            if existing is None:
                student_bundles[student.student_id] = (student, [group])
                continue
            existing_student, student_groups = existing
            if (
                existing_student.email.casefold() != student.email.casefold()
                or (existing_student.github_login or "").casefold()
                != (student.github_login or "").casefold()
            ):
                raise InputValidationError(
                    f"Student {student.student_id!r} has conflicting identities "
                    "across desired groups"
                )
            student_groups.append(group)

    for student_id in sorted(student_bundles):
        student, student_groups = student_bundles[student_id]
        ordered_groups = sorted(
            student_groups,
            key=lambda item: (item.individual, item.key),
        )
        deduplicated_groups: list[DesiredGroup] = []
        seen_team_names: set[str] = set()
        for group in ordered_groups:
            team_key = group.team_name.casefold()
            if team_key in seen_team_names:
                continue
            seen_team_names.add(team_key)
            deduplicated_groups.append(group)
        ordered_groups = deduplicated_groups

        shared_groups = [group for group in ordered_groups if not group.individual]
        if shared_groups and not retry_expired:
            identity = _resolve_student_identity(config, student, snapshot)
            if identity.state == IdentityResolutionState.RESOLVED and identity.member is not None:
                for shared_group in shared_groups:
                    shared_team = teams_by_name.get(shared_group.team_name)
                    mapping_conflict = _membership_mapping_conflict(
                        student,
                        shared_group,
                        shared_team,
                        snapshot,
                    )
                    if mapping_conflict is not None:
                        student_actions.append(
                            Action(
                                action_id=_membership_action_id(
                                    shared_group,
                                    student,
                                ),
                                action_type=ActionType.REVIEW_REQUIRED,
                                scope=shared_group.key,
                                student_id=student.student_id,
                                email=student.email,
                                github_login=identity.member.login,
                                github_user_id=identity.member.id,
                                group_id=shared_group.group_id,
                                team_name=shared_group.team_name,
                                team_slug=(shared_team.slug if shared_team else None),
                                team_id=shared_team.id if shared_team else None,
                                identity_team_name=identity.team_name,
                                identity_team_slug=identity.team_slug,
                                identity_team_id=identity.team_id,
                                invitation_state=InvitationState.UNRESOLVED,
                                desired_state=(
                                    "resolved GitHub account is an active shared team member"
                                ),
                                reason=mapping_conflict,
                                status=ActionStatus.REVIEW,
                            )
                        )
                        continue

                    existing_membership = next(
                        (
                            member
                            for member in _members_for_team(
                                snapshot,
                                shared_team,
                            )
                            if member.id == identity.member.id
                        ),
                        None,
                    )
                    if existing_membership is not None:
                        student_actions.append(
                            Action(
                                action_id=_membership_action_id(
                                    shared_group,
                                    student,
                                ),
                                action_type=ActionType.SKIP_ACCEPTED,
                                scope=shared_group.key,
                                student_id=student.student_id,
                                email=student.email,
                                github_login=existing_membership.login,
                                github_user_id=existing_membership.id,
                                group_id=shared_group.group_id,
                                team_name=shared_group.team_name,
                                team_slug=(shared_team.slug if shared_team else None),
                                team_id=shared_team.id if shared_team else None,
                                identity_team_name=identity.team_name,
                                identity_team_slug=identity.team_slug,
                                identity_team_id=identity.team_id,
                                invitation_state=(InvitationState.ACCEPTED_CONFIRMED),
                                current_state=(
                                    "resolved GitHub user is already active in "
                                    f"the shared team as {existing_membership.role}"
                                ),
                                desired_state=(
                                    "resolved GitHub account is an active shared team member"
                                ),
                                reason=(
                                    "the individual team resolved the student, "
                                    "and the same numeric GitHub user ID is "
                                    "already active in the shared team"
                                ),
                                status=ActionStatus.SKIPPED,
                            )
                        )
                        continue

                    student_actions.append(
                        Action(
                            action_id=_membership_action_id(
                                shared_group,
                                student,
                            ),
                            action_type=ActionType.ADD_TEAM_MEMBER,
                            scope=shared_group.key,
                            student_id=student.student_id,
                            email=student.email,
                            github_login=identity.member.login,
                            github_user_id=identity.member.id,
                            group_id=shared_group.group_id,
                            team_name=shared_group.team_name,
                            team_slug=shared_team.slug if shared_team else None,
                            team_id=shared_team.id if shared_team else None,
                            identity_team_name=identity.team_name,
                            identity_team_slug=identity.team_slug,
                            identity_team_id=identity.team_id,
                            invitation_state=(InvitationState.ACCEPTED_CONFIRMED),
                            current_state=("resolved GitHub user is not active in the shared team"),
                            desired_state=(
                                "resolved GitHub account is an active shared team member"
                            ),
                            reason=(f"{identity.reason}; no organisation invitation is required"),
                            dependencies=list(group_dependencies[shared_group.key]),
                        )
                    )
                continue

            explicit_empty_identity = (
                any(group.individual for group in ordered_groups)
                and (identity_team := teams_by_name.get(identity.team_name)) is not None
                and not _members_for_team(snapshot, identity_team)
                and not any(
                    record.team_name.casefold() == identity.team_name.casefold()
                    or (
                        record.student_id == student.student_id
                        and record.group_id == f"IND-{student.student_id}"
                    )
                    for record in snapshot.ledger.records
                )
            )
            if (
                identity.state == IdentityResolutionState.UNRESOLVED
                and not explicit_empty_identity
                and not _pending_contains_expected_bundle(
                    student,
                    ordered_groups,
                    snapshot,
                )
            ):
                primary_group = shared_groups[0]
                primary_team = teams_by_name.get(primary_group.team_name)
                pending_invitation = snapshot.pending_by_email.get(student.email.casefold())
                student_actions.append(
                    Action(
                        action_id=_membership_action_id(primary_group, student),
                        action_type=ActionType.REVIEW_REQUIRED,
                        scope=primary_group.key,
                        student_id=student.student_id,
                        email=student.email,
                        github_login=student.github_login,
                        group_id=primary_group.group_id,
                        team_name=primary_group.team_name,
                        team_slug=primary_team.slug if primary_team else None,
                        team_id=primary_team.id if primary_team else None,
                        identity_team_name=identity.team_name,
                        identity_team_slug=identity.team_slug,
                        identity_team_id=identity.team_id,
                        invitation_id=(
                            pending_invitation.id if pending_invitation is not None else None
                        ),
                        invitation_state=InvitationState.UNRESOLVED,
                        desired_state=(
                            "student identity safely resolved before shared team assignment"
                        ),
                        reason=(
                            f"{identity.reason}; no replacement organisation "
                            "invitation or direct membership write will be sent"
                        ),
                        status=ActionStatus.REVIEW,
                    )
                )
                continue

        decision = reconcile_invitation_bundle(student, ordered_groups, snapshot)
        pending_invitation = snapshot.pending_by_email.get(student.email.casefold())
        action_type = decision.action_type
        status = (
            ActionStatus.PLANNED
            if action_type == ActionType.SEND_INVITATION
            else ActionStatus.SKIPPED
        )
        reason = decision.reason
        if retry_expired:
            if decision.state == InvitationState.EXPIRED:
                if len(ordered_groups) == 1 and _record_is_part_of_multi_team_invitation(
                    student,
                    ordered_groups[0],
                    snapshot,
                ):
                    action_type = ActionType.REVIEW_REQUIRED
                    status = ActionStatus.REVIEW
                    reason = (
                        f"{decision.reason}; this record belongs to a multi-team "
                        "invitation and must be retried with --add-individual"
                    )
                else:
                    action_type = ActionType.SEND_INVITATION
                    status = ActionStatus.PLANNED
                    reason = f"{decision.reason}; this dedicated retry command may resend it"
            elif decision.state == InvitationState.PENDING:
                action_type = decision.action_type
                status = (
                    ActionStatus.REVIEW
                    if action_type == ActionType.REVIEW_REQUIRED
                    else ActionStatus.SKIPPED
                )
            elif decision.state in {
                InvitationState.ACCEPTED_CONFIRMED,
                InvitationState.ACCEPTED_INFERRED,
            }:
                action_type = ActionType.SKIP_ACCEPTED
                status = ActionStatus.SKIPPED
            elif decision.state in {
                InvitationState.UNRESOLVED,
                InvitationState.FAILED,
            }:
                action_type = ActionType.REVIEW_REQUIRED
                status = ActionStatus.REVIEW
            else:
                action_type = ActionType.SKIP_UNCHANGED
                status = ActionStatus.SKIPPED
                reason = "only explicitly expired invitations are eligible for retry"
        elif decision.state in {
            InvitationState.EXPIRED,
            InvitationState.UNRESOLVED,
            InvitationState.FAILED,
        }:
            action_type = ActionType.REVIEW_REQUIRED
            status = ActionStatus.REVIEW
        elif action_type == ActionType.REVIEW_REQUIRED:
            status = ActionStatus.REVIEW

        primary_group = ordered_groups[0]
        primary_team = teams_by_name.get(primary_group.team_name)
        invitation_targets = [
            InvitationTarget(
                scope=group.key,
                group_id=group.group_id,
                team_name=group.team_name,
                team_slug=(
                    target_team.slug
                    if (target_team := teams_by_name.get(group.team_name))
                    else None
                ),
                team_id=target_team.id if target_team else None,
                individual=group.individual,
            )
            for group in ordered_groups
        ]
        invitation_dependencies: list[str] = []
        if action_type == ActionType.SEND_INVITATION:
            for group in ordered_groups:
                for dependency in group_dependencies[group.key]:
                    if dependency not in invitation_dependencies:
                        invitation_dependencies.append(dependency)

        student_actions.append(
            Action(
                action_id=_invitation_action_id(primary_group, student),
                action_type=action_type,
                scope=primary_group.key,
                student_id=student.student_id,
                email=student.email,
                github_login=student.github_login,
                group_id=primary_group.group_id,
                team_name=primary_group.team_name,
                team_slug=primary_team.slug if primary_team else None,
                team_id=primary_team.id if primary_team else None,
                invitation_id=(pending_invitation.id if pending_invitation is not None else None),
                invitation_created_at=(
                    pending_invitation.created_at if pending_invitation is not None else None
                ),
                pending_team_ids=(
                    sorted(
                        snapshot.invitation_team_ids.get(
                            pending_invitation.id,
                            set(),
                        )
                    )
                    if pending_invitation is not None
                    else []
                ),
                invitation_targets=invitation_targets,
                invitation_state=decision.state,
                desired_state=(
                    "organisation member assigned to every expected team"
                    if len(invitation_targets) > 1
                    else "organisation member assigned to expected team"
                ),
                reason=reason,
                dependencies=invitation_dependencies,
                status=status,
            )
        )

    plan = Plan(
        title=title,
        organisation=config.organisation,
        subject=config.subject,
        term=config.term,
        mode=mode,
        generated_at=now,
        actions=[
            *team_actions,
            *repository_actions,
            *permission_actions,
            *student_actions,
        ],
    )
    attach_execution_estimate(plan, config, snapshot.organisation)
    return plan


def build_semester_close_plan(
    config: Configuration,
    groups: Sequence[DesiredGroup],
    snapshot: Snapshot,
    *,
    archive_repositories: bool,
    remove_team_access: bool,
    mode: str,
    generated_at: datetime | None = None,
) -> Plan:
    if not archive_repositories and not remove_team_access:
        raise InputValidationError(
            "Semester close requires --archive-repositories, --remove-team-access, or both"
        )

    teams_by_name = snapshot.teams_by_name
    repositories_by_name = snapshot.repositories_by_name
    remove_actions: list[Action] = []
    archive_actions: list[Action] = []
    for group in groups:
        team = teams_by_name.get(group.team_name)
        for desired_repository in group.repositories:
            repository = repositories_by_name.get(desired_repository.name)
            remove_action_id = f"{group.key}:close:remove:{desired_repository.name.casefold()}"
            archive_action_id = f"{group.key}:close:archive:{desired_repository.name.casefold()}"
            remove_dependency: list[str] = []

            if remove_team_access:
                if team is None:
                    remove_actions.append(
                        _skipped_action(
                            action_id=remove_action_id,
                            scope=group.key,
                            group=group,
                            repository=desired_repository.name,
                            current_state="team is absent",
                            desired_state="no team repository relationship",
                            reason="the expected team does not exist",
                        )
                    )
                elif repository is None:
                    remove_actions.append(
                        _skipped_action(
                            action_id=remove_action_id,
                            scope=group.key,
                            group=group,
                            team=team,
                            repository=desired_repository.name,
                            current_state="repository is absent",
                            desired_state="no team repository relationship",
                            reason="the expected repository does not exist",
                        )
                    )
                elif repository.is_archived:
                    permission = snapshot.permissions.get(
                        permission_key(team.slug, repository.name)
                    )
                    if permission is None:
                        remove_actions.append(
                            _skipped_action(
                                action_id=remove_action_id,
                                scope=group.key,
                                group=group,
                                team=team,
                                repository=repository.name,
                                current_state="no team relationship",
                                desired_state="no team relationship",
                                reason="team access is already absent",
                            )
                        )
                    else:
                        remove_actions.append(
                            Action(
                                action_id=remove_action_id,
                                action_type=ActionType.ERROR,
                                scope=group.key,
                                group_id=group.group_id,
                                team_name=group.team_name,
                                team_slug=team.slug,
                                team_id=team.id,
                                repository=repository.name,
                                current_state=(
                                    f"archived repository with {permission} team access"
                                ),
                                desired_state="no team relationship",
                                reason=(
                                    "GitHub does not allow team access to be changed "
                                    "after archival, and repositories are never "
                                    "unarchived automatically"
                                ),
                                destructive=True,
                                status=ActionStatus.FAILED,
                            )
                        )
                else:
                    permission = snapshot.permissions.get(
                        permission_key(team.slug, repository.name)
                    )
                    if permission is None:
                        remove_actions.append(
                            _skipped_action(
                                action_id=remove_action_id,
                                scope=group.key,
                                group=group,
                                team=team,
                                repository=repository.name,
                                current_state="no team relationship",
                                desired_state="no team relationship",
                                reason="team access is already absent",
                            )
                        )
                    else:
                        remove_actions.append(
                            Action(
                                action_id=remove_action_id,
                                action_type=ActionType.REMOVE_TEAM_REPOSITORY,
                                scope=group.key,
                                group_id=group.group_id,
                                team_name=group.team_name,
                                team_slug=team.slug,
                                team_id=team.id,
                                repository=repository.name,
                                current_state=permission,
                                desired_state="no team relationship",
                                reason="semester closure requested removal of team access",
                                destructive=True,
                            )
                        )
                remove_dependency = [remove_action_id]

            if archive_repositories:
                if repository is None:
                    archive_actions.append(
                        _skipped_action(
                            action_id=archive_action_id,
                            scope=group.key,
                            group=group,
                            repository=desired_repository.name,
                            current_state="repository is absent",
                            desired_state="repository archived",
                            reason="the expected repository does not exist",
                        )
                    )
                elif repository.is_archived:
                    archive_actions.append(
                        _skipped_action(
                            action_id=archive_action_id,
                            scope=group.key,
                            group=group,
                            team=team,
                            repository=repository.name,
                            current_state="archived",
                            desired_state="archived",
                            reason="the repository is already archived",
                        )
                    )
                else:
                    archive_actions.append(
                        Action(
                            action_id=archive_action_id,
                            action_type=ActionType.ARCHIVE_REPOSITORY,
                            scope=group.key,
                            group_id=group.group_id,
                            team_name=group.team_name,
                            team_slug=team.slug if team else None,
                            team_id=team.id if team else None,
                            repository=repository.name,
                            current_state="active",
                            desired_state="archived",
                            reason="semester closure requested repository archival",
                            dependencies=remove_dependency,
                            destructive=True,
                        )
                    )

    plan = Plan(
        title="GitHub Semester Close Plan",
        organisation=config.organisation,
        subject=config.subject,
        term=config.term,
        mode=mode,
        generated_at=generated_at or utc_now(),
        # GitHub forbids collaborator/team changes after archival, so access is
        # removed first and archival depends on that removal.
        actions=[*remove_actions, *archive_actions],
    )
    attach_execution_estimate(plan, config, snapshot.organisation)
    return plan


def _parse_github_datetime(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    normalised = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _action_invitation_targets(action: Action) -> list[InvitationTarget]:
    if action.invitation_targets:
        return action.invitation_targets
    if action.group_id is None or action.team_name is None:
        raise RuntimeError(f"Invitation action {action.action_id} has no team targets")
    return [
        InvitationTarget(
            scope=action.scope,
            group_id=action.group_id,
            team_name=action.team_name,
            team_slug=action.team_slug,
            team_id=action.team_id,
            individual=action.scope.startswith("individual:"),
        )
    ]


def _action_student(
    action: Action,
    target: InvitationTarget | None = None,
) -> Student:
    group_id = target.group_id if target is not None else action.group_id
    if action.student_id is None or action.email is None or group_id is None:
        raise RuntimeError(f"Invitation action {action.action_id} is incomplete")
    return Student(
        student_id=action.student_id,
        email=action.email,
        group_id=group_id,
        github_login=action.github_login,
    )


def _action_group(
    action: Action,
    target: InvitationTarget | None = None,
) -> DesiredGroup:
    group_id = target.group_id if target is not None else action.group_id
    team_name = target.team_name if target is not None else action.team_name
    scope = target.scope if target is not None else action.scope
    if group_id is None or team_name is None:
        raise RuntimeError(f"Action {action.action_id} has no group or team")
    return DesiredGroup(
        key=scope,
        group_id=group_id,
        team_name=team_name,
        repositories=[],
        students=[],
        individual=(
            target.individual if target is not None else action.scope.startswith("individual:")
        ),
    )


def _seed_team_lookup(actions: Sequence[Action]) -> dict[str, Team]:
    teams: dict[str, Team] = {}
    for action in actions:
        if (
            action.team_name is not None
            and action.team_slug is not None
            and action.team_id is not None
        ):
            teams[action.team_name] = Team(
                id=action.team_id,
                name=action.team_name,
                slug=action.team_slug,
            )
        for target in action.invitation_targets:
            if target.team_slug is not None and target.team_id is not None:
                teams[target.team_name] = Team(
                    id=target.team_id,
                    name=target.team_name,
                    slug=target.team_slug,
                )
    return teams


def _resolve_invitation_teams(
    action: Action,
    teams: Mapping[str, Team],
) -> list[tuple[InvitationTarget, Team]]:
    resolved: list[tuple[InvitationTarget, Team]] = []
    for target in _action_invitation_targets(action):
        team = teams.get(target.team_name)
        if team is None:
            raise RuntimeError(
                f"Resolved team {target.team_name!r} is unavailable for {action.action_id}"
            )
        target.team_id = team.id
        target.team_slug = team.slug
        resolved.append((target, team))
    if not resolved:
        raise RuntimeError(f"Invitation action {action.action_id} has no resolved teams")
    primary_target, primary_team = resolved[0]
    action.group_id = primary_target.group_id
    action.team_name = primary_target.team_name
    action.team_id = primary_team.id
    action.team_slug = primary_team.slug
    return resolved


def _shared_invitation_attempt_count(
    ledger: InvitationLedger,
    action: Action,
) -> int:
    existing = [
        ledger.find(action.email or "", target.team_name)
        for target in _action_invitation_targets(action)
    ]
    return (
        max(
            (record.attempt_count for record in existing if record is not None),
            default=0,
        )
        + 1
    )


def _record_successful_invitation_bundle(
    ledger: InvitationLedger,
    *,
    action: Action,
    resolved: Sequence[tuple[InvitationTarget, Team]],
    invitation: Invitation,
    now: datetime,
) -> None:
    attempt_count = _shared_invitation_attempt_count(ledger, action)
    for target, team in resolved:
        record_successful_invitation(
            ledger,
            student=_action_student(action, target),
            group=_action_group(action, target),
            team=team,
            invitation=invitation,
            now=now,
            attempt_count=attempt_count,
        )


def _record_failed_invitation_bundle(
    ledger: InvitationLedger,
    *,
    action: Action,
    resolved: Sequence[tuple[InvitationTarget, Team]],
    reason: str,
    now: datetime,
) -> None:
    attempt_count = _shared_invitation_attempt_count(ledger, action)
    for target, team in resolved:
        record_failed_invitation(
            ledger,
            student=_action_student(action, target),
            group=_action_group(action, target),
            team=team,
            reason=reason,
            now=now,
            attempt_count=attempt_count,
        )


def _adopt_pending_after_invitation_error(
    client: GitHubClient,
    organisation: str,
    email: str,
) -> Invitation | None:
    invitations = client.list_pending_invitations(organisation)
    email_key = email.casefold()
    for invitation in invitations:
        if invitation.email is None or invitation.email.casefold() != email_key:
            continue
        team_ids = client.list_invitation_team_ids(
            organisation,
            invitation.id,
        )
        return Invitation(
            id=invitation.id,
            email=invitation.email,
            login=invitation.login,
            role=invitation.role,
            created_at=invitation.created_at,
            team_ids=tuple(sorted(team_ids)),
        )
    return None


def _run_paced_write(
    pacer: ExecutionPacer | None,
    *,
    action_type: ActionType,
    invitation: bool = False,
    operation: Callable[[], Any],
) -> Any:
    if pacer is None:
        return operation()
    phase = action_type.value.casefold().replace("_", " ")
    while True:
        try:
            pacer.before_write(invitation=invitation)
        except ExecutionLimitError:
            pacer.finish_action(phase=phase, succeeded=False)
            raise
        try:
            result = operation()
        except GitHubRateLimitError as exc:
            pacer.finish_attempt()
            try:
                pacer.handle_remote_limit(exc, invitation=invitation)
            except ExecutionLimitError:
                pacer.finish_action(phase=phase, succeeded=False)
                raise
            continue
        except Exception:
            pacer.finish_attempt()
            pacer.finish_action(phase=phase, succeeded=False)
            raise
        pacer.finish_attempt()
        pacer.finish_action(phase=phase, succeeded=True)
        return result


def execute_plan(
    plan: Plan,
    *,
    client: GitHubClient,
    config: Configuration,
    ledger: InvitationLedger,
    ledger_file: Path,
    now: Callable[[], datetime] = utc_now,
    pacer: ExecutionPacer | None = None,
) -> ExecutionOutcome:
    if plan.mode.casefold() != "apply":
        raise InputValidationError("Refusing to execute a plan that is not in Apply mode")

    executed = plan.model_copy(deep=True)
    outcomes: dict[str, bool] = {}
    teams = _seed_team_lookup(executed.actions)
    successful_writes = 0
    rate_limited = False
    auth_failed = False
    fatal_ledger_failure = False
    unexpected_failure = False
    local_failures = 0

    for action in executed.actions:
        if fatal_ledger_failure or rate_limited or auth_failed:
            if action.is_write and action.status == ActionStatus.PLANNED:
                action.status = ActionStatus.BLOCKED
                action.error = "a prior fatal error stopped further writes"
                outcomes[action.action_id] = False
            else:
                outcomes.setdefault(
                    action.action_id,
                    action.status in {ActionStatus.SKIPPED, ActionStatus.SUCCEEDED},
                )
            continue

        if (
            action.invitation_state == InvitationState.PENDING
            and action.invitation_id is not None
            and action.action_type
            in {
                ActionType.SKIP_PENDING_INVITATION,
                ActionType.REVIEW_REQUIRED,
            }
            and action.status in {ActionStatus.SKIPPED, ActionStatus.REVIEW}
        ):
            targets = _action_invitation_targets(action)
            target_teams = [(target, teams.get(target.team_name)) for target in targets]
            mapping_conflicts = False
            for target, expected_team in target_teams:
                existing_record = (
                    ledger.find(action.email, target.team_name)
                    if action.email is not None
                    else None
                )
                if existing_record is not None and (
                    existing_record.student_id != action.student_id
                    or existing_record.group_id != target.group_id
                    or (
                        existing_record.team_id is not None
                        and (expected_team is None or existing_record.team_id != expected_team.id)
                    )
                ):
                    mapping_conflicts = True
                    break
            if mapping_conflicts:
                action.status = ActionStatus.REVIEW
                action.reason = (
                    f"{action.reason}; the existing ledger identity or team "
                    "mapping conflicts with the roster"
                )
                outcomes[action.action_id] = False
                continue

            attached = [
                team is not None and team.id in action.pending_team_ids
                for _target, team in target_teams
            ]
            complete_bundle = all(attached)
            already_recorded = all(
                (existing_record := (ledger.find(action.email or "", target.team_name))) is not None
                and existing_record.status
                == (InvitationState.PENDING if target_attached else InvitationState.FAILED)
                and existing_record.invitation_id == action.invitation_id
                and (existing_record.team_id == (team.id if team is not None else None))
                for (target, team), target_attached in zip(
                    target_teams,
                    attached,
                    strict=True,
                )
            )
            if already_recorded:
                action.status = ActionStatus.SKIPPED if complete_bundle else ActionStatus.REVIEW
                outcomes[action.action_id] = complete_bundle
                continue

            existing_records = [
                ledger.find(action.email or "", target.team_name) for target in targets
            ]
            prior_attempt_count = max(
                (record.attempt_count for record in existing_records if record is not None),
                default=0,
            )
            invitation_changed = any(
                record is not None and record.invitation_id != action.invitation_id
                for record in existing_records
            )
            observed_attempt_count = max(
                1,
                prior_attempt_count + (1 if invitation_changed else 0),
            )
            try:
                for target, expected_team in target_teams:
                    record_observed_pending_invitation(
                        ledger,
                        action=action,
                        target=target,
                        team=expected_team,
                        now=now(),
                        attempt_count=observed_attempt_count,
                    )
                save_ledger_atomic(ledger_file, ledger)
            except Exception as exc:
                fatal_ledger_failure = True
                local_failures += 1
                action.status = ActionStatus.FAILED
                action.error = (
                    "the pending invitation was observed remotely, but its "
                    f"ledger records could not be saved: {exc}"
                )
                outcomes[action.action_id] = False
                continue
            action.status = ActionStatus.SUCCEEDED if complete_bundle else ActionStatus.REVIEW
            action.reason = (
                f"{action.reason}; the observed invitation assignments were "
                "recorded in the local ledger"
            )
            outcomes[action.action_id] = complete_bundle
            continue
        if action.status == ActionStatus.SKIPPED:
            outcomes[action.action_id] = True
            continue
        if action.status in {ActionStatus.FAILED, ActionStatus.REVIEW}:
            outcomes[action.action_id] = False
            continue
        failed_dependencies = [
            dependency for dependency in action.dependencies if not outcomes.get(dependency, False)
        ]
        if failed_dependencies:
            action.status = ActionStatus.BLOCKED
            action.error = "blocked by failed prerequisite(s): " + ", ".join(failed_dependencies)
            outcomes[action.action_id] = False
            continue
        if not action.is_write:
            action.status = ActionStatus.SKIPPED
            outcomes[action.action_id] = True
            continue

        resolved_invitation_teams: list[tuple[InvitationTarget, Team]] = []
        try:
            if action.action_type == ActionType.CREATE_TEAM:
                if action.team_name is None:
                    raise RuntimeError("CREATE_TEAM action has no team name")
                team_name = action.team_name
                team = _run_paced_write(
                    pacer,
                    action_type=action.action_type,
                    operation=partial(
                        client.create_team,
                        config.organisation,
                        team_name,
                    ),
                )
                if team.name != action.team_name:
                    raise RuntimeError(
                        f"GitHub returned an unexpected name for the created team: {team.name!r}"
                    )
                teams[action.team_name] = team
                action.team_id = team.id
                action.team_slug = team.slug
            elif action.action_type == ActionType.CREATE_REPOSITORY:
                if action.repository is None:
                    raise RuntimeError("CREATE_REPOSITORY action has no repository name")
                repository_name = action.repository
                repository_description = action.description or ""
                created_repository = _run_paced_write(
                    pacer,
                    action_type=action.action_type,
                    operation=partial(
                        client.create_repository_from_template,
                        config.template_owner,
                        config.template_repository,
                        config.organisation,
                        repository_name,
                        repository_description,
                    ),
                )
                if created_repository.name != action.repository:
                    raise RuntimeError(
                        "GitHub returned an unexpected name for the created "
                        f"repository: {created_repository.name!r}"
                    )
                if not created_repository.is_private:
                    raise RuntimeError(f"GitHub created repository {action.repository!r} as public")
                if created_repository.is_archived:
                    raise RuntimeError(
                        f"GitHub created repository {action.repository!r} as archived"
                    )
            elif action.action_type in {
                ActionType.GRANT_TEAM_REPOSITORY,
                ActionType.UPDATE_TEAM_REPOSITORY_PERMISSION,
            }:
                if action.team_name is None or action.repository is None:
                    raise RuntimeError(f"{action.action_type.value} action is incomplete")
                resolved_team = teams.get(action.team_name)
                if resolved_team is None:
                    raise RuntimeError(f"Resolved team is unavailable for {action.action_id}")
                action.team_id = resolved_team.id
                action.team_slug = resolved_team.slug
                resolved_team_slug = resolved_team.slug
                repository_name = action.repository
                repository_permission = config.repositories.permission.value
                _run_paced_write(
                    pacer,
                    action_type=action.action_type,
                    operation=partial(
                        client.set_team_repository_permission,
                        config.organisation,
                        resolved_team_slug,
                        repository_name,
                        repository_permission,
                    ),
                )
            elif action.action_type == ActionType.ADD_TEAM_MEMBER:
                if (
                    action.team_name is None
                    or action.identity_team_name is None
                    or action.identity_team_id is None
                    or action.github_user_id is None
                ):
                    raise RuntimeError(
                        "ADD_TEAM_MEMBER action lacks a resolved source identity "
                        "or destination team"
                    )

                live_teams = {team.name: team for team in client.list_teams(config.organisation)}
                identity_team = live_teams.get(action.identity_team_name)
                if identity_team is None or identity_team.id != action.identity_team_id:
                    raise GitHubResponseError(
                        "the individual identity team is missing or has a "
                        "different numeric ID; no membership write was attempted",
                        operation=(f"revalidate identity team {action.identity_team_name}"),
                    )
                action.identity_team_slug = identity_team.slug
                identity_candidates = _direct_ordinary_members(
                    client.list_team_members(
                        config.organisation,
                        identity_team.slug,
                    )
                )
                if (
                    len(identity_candidates) != 1
                    or identity_candidates[0].id != action.github_user_id
                ):
                    raise GitHubResponseError(
                        "the individual identity team no longer contains the "
                        "same sole direct active non-maintainer member; no "
                        "membership write was attempted",
                        operation=(f"revalidate identity member for {action.identity_team_name}"),
                    )

                identity_member = identity_candidates[0]
                action.github_login = identity_member.login
                planned_team = teams.get(action.team_name)
                live_target_team = live_teams.get(action.team_name)
                if (
                    planned_team is not None
                    and live_target_team is not None
                    and planned_team.id != live_target_team.id
                ):
                    raise GitHubResponseError(
                        "the destination shared team has a different numeric "
                        "ID from the completed plan; no membership write was "
                        "attempted",
                        operation=(f"revalidate destination team {action.team_name}"),
                    )
                resolved_team = live_target_team or planned_team
                if resolved_team is None:
                    raise RuntimeError(f"Resolved team is unavailable for {action.action_id}")
                action.team_id = resolved_team.id
                action.team_slug = resolved_team.slug
                target_members = client.list_team_members(
                    config.organisation,
                    resolved_team.slug,
                )
                if any(member.id == action.github_user_id for member in target_members):
                    action.status = ActionStatus.SKIPPED
                    action.reason = (
                        "the resolved GitHub user became active in the shared "
                        "team before this action executed"
                    )
                    outcomes[action.action_id] = True
                    continue

                resolved_team_slug = resolved_team.slug
                github_login = identity_member.login
                membership = _run_paced_write(
                    pacer,
                    action_type=action.action_type,
                    operation=partial(
                        client.add_team_member,
                        config.organisation,
                        resolved_team_slug,
                        github_login,
                    ),
                )
                if membership.state != "active":
                    raise GitHubResponseError(
                        "GitHub returned a pending team membership even though "
                        "the individual team proves the account is already an "
                        "organisation member",
                        operation=(
                            f"add {identity_member.login} to team "
                            f"{config.organisation}/{resolved_team.slug}"
                        ),
                    )
            elif action.action_type == ActionType.SEND_INVITATION:
                if action.email is None:
                    raise RuntimeError("SEND_INVITATION action is incomplete")
                resolved_invitation_teams = _resolve_invitation_teams(
                    action,
                    teams,
                )
                invitation_email = action.email
                invitation_team_ids = list(
                    dict.fromkeys(team.id for _target, team in resolved_invitation_teams)
                )
                invitation = _run_paced_write(
                    pacer,
                    action_type=action.action_type,
                    invitation=True,
                    operation=partial(
                        client.invite_member,
                        config.organisation,
                        invitation_email,
                        invitation_team_ids,
                    ),
                )
                invitation_time = _parse_github_datetime(
                    invitation.created_at,
                    now(),
                )
                normalised_invitation = Invitation(
                    id=invitation.id,
                    email=invitation.email,
                    login=invitation.login,
                    role=invitation.role,
                    created_at=format_timestamp(invitation_time),
                    team_ids=invitation.team_ids,
                )
                _record_successful_invitation_bundle(
                    ledger,
                    action=action,
                    resolved=resolved_invitation_teams,
                    invitation=normalised_invitation,
                    now=now(),
                )
                try:
                    save_ledger_atomic(ledger_file, ledger)
                except Exception as exc:
                    fatal_ledger_failure = True
                    raise RuntimeError(
                        "the invitation may exist remotely, but its ledger record "
                        f"could not be saved: {exc}"
                    ) from exc
            elif action.action_type == ActionType.REMOVE_TEAM_REPOSITORY:
                if action.team_slug is None or action.repository is None:
                    raise RuntimeError("REMOVE_TEAM_REPOSITORY action is incomplete")
                team_slug = action.team_slug
                repository_name = action.repository
                _run_paced_write(
                    pacer,
                    action_type=action.action_type,
                    operation=partial(
                        client.remove_team_repository,
                        config.organisation,
                        team_slug,
                        repository_name,
                    ),
                )
            elif action.action_type == ActionType.ARCHIVE_REPOSITORY:
                if action.repository is None:
                    raise RuntimeError("ARCHIVE_REPOSITORY action is incomplete")
                repository_name = action.repository
                _run_paced_write(
                    pacer,
                    action_type=action.action_type,
                    operation=partial(
                        client.archive_repository,
                        config.organisation,
                        repository_name,
                    ),
                )
            else:
                raise RuntimeError(f"Executor does not support {action.action_type.value}")
        except (GitHubAuthError, GitHubRateLimitError, ExecutionLimitError) as exc:
            action.status = ActionStatus.FAILED
            action.error = str(exc)
            outcomes[action.action_id] = False
            if isinstance(exc, (GitHubRateLimitError, ExecutionLimitError)):
                rate_limited = True
            else:
                auth_failed = True
            continue
        except GitHubError as exc:
            adopted: Invitation | None = None
            failure_reason = str(exc)
            if (
                isinstance(exc, GitHubNetworkError)
                and action.action_type == ActionType.ADD_TEAM_MEMBER
                and action.team_slug is not None
                and action.github_user_id is not None
            ):
                try:
                    recovered_members = client.list_team_members(
                        config.organisation,
                        action.team_slug,
                    )
                except GitHubError:
                    recovered_members = []
                if any(member.id == action.github_user_id for member in recovered_members):
                    action.status = ActionStatus.SUCCEEDED
                    action.reason = (
                        "the membership request returned a network error, but "
                        "the expected numeric GitHub user ID is active in the "
                        "destination team"
                    )
                    outcomes[action.action_id] = True
                    successful_writes += 1
                    continue
            if (
                isinstance(exc, GitHubNetworkError)
                and action.action_type == ActionType.SEND_INVITATION
                and action.email is not None
                and resolved_invitation_teams
            ):
                try:
                    adopted = _adopt_pending_after_invitation_error(
                        client,
                        config.organisation,
                        action.email,
                    )
                except GitHubError:
                    adopted = None
                expected_team_ids = {team.id for _target, team in resolved_invitation_teams}
                if adopted is not None and expected_team_ids.issubset(adopted.team_ids):
                    _record_successful_invitation_bundle(
                        ledger,
                        action=action,
                        resolved=resolved_invitation_teams,
                        invitation=adopted,
                        now=now(),
                    )
                    try:
                        save_ledger_atomic(ledger_file, ledger)
                    except Exception as ledger_exc:
                        fatal_ledger_failure = True
                        local_failures += 1
                        action.status = ActionStatus.FAILED
                        action.error = (
                            f"{exc}; a pending invitation was discovered, "
                            "but its ledger record could not be saved: "
                            f"{ledger_exc}"
                        )
                        outcomes[action.action_id] = False
                        continue
                    action.status = ActionStatus.SUCCEEDED
                    action.reason = (
                        "the invitation request returned an error, but a matching "
                        "pending invitation was discovered and recorded"
                    )
                    outcomes[action.action_id] = True
                    successful_writes += 1
                    continue
                if adopted is not None:
                    failure_reason = (
                        f"{failure_reason}; a matching pending invitation was "
                        "found, but it did not contain every expected team"
                    )
            if resolved_invitation_teams:
                try:
                    _record_failed_invitation_bundle(
                        ledger,
                        action=action,
                        resolved=resolved_invitation_teams,
                        reason=failure_reason,
                        now=now(),
                    )
                    save_ledger_atomic(ledger_file, ledger)
                except Exception as ledger_exc:
                    fatal_ledger_failure = True
                    local_failures += 1
                    failure_reason = (
                        f"{failure_reason}; the failed invitation could not be "
                        f"saved to the local ledger: {ledger_exc}"
                    )
            action.status = ActionStatus.FAILED
            action.error = failure_reason
            outcomes[action.action_id] = False
            continue
        except Exception as exc:
            action.status = ActionStatus.FAILED
            action.error = str(exc)
            outcomes[action.action_id] = False
            if fatal_ledger_failure:
                continue
            unexpected_failure = True
            continue

        action.status = ActionStatus.SUCCEEDED
        outcomes[action.action_id] = True
        successful_writes += 1

    failed_writes = sum(
        action.is_write and action.status == ActionStatus.FAILED for action in executed.actions
    )
    blocked_writes = sum(
        action.is_write and action.status == ActionStatus.BLOCKED for action in executed.actions
    )
    planning_errors = any(action.action_type == ActionType.ERROR for action in executed.actions)
    if rate_limited:
        exit_code = EXIT_RATE_LIMIT
    elif auth_failed:
        exit_code = EXIT_AUTH
    elif successful_writes == 0 and (unexpected_failure or (local_failures and failed_writes == 0)):
        exit_code = EXIT_UNEXPECTED
    elif failed_writes or blocked_writes or planning_errors or local_failures:
        exit_code = EXIT_PARTIAL
    else:
        exit_code = EXIT_SUCCESS
    if pacer is not None:
        executed.execution_metrics = pacer.metrics
    return ExecutionOutcome(
        plan=executed,
        exit_code=exit_code,
        successful_writes=successful_writes,
        failed_writes=failed_writes,
        blocked_writes=blocked_writes,
    )


def verify_execution(
    outcome: ExecutionOutcome,
    *,
    client: GitHubClient,
    config: Configuration,
) -> ExecutionOutcome:
    """Perform a read-only post-apply verification of successful writes."""

    if outcome.successful_writes == 0 or outcome.exit_code in {
        EXIT_AUTH,
        EXIT_RATE_LIMIT,
    }:
        return outcome
    plan = outcome.plan.model_copy(deep=True)
    verification_errors: list[Action] = []
    try:
        teams = {team.name: team for team in client.list_teams(config.organisation)}
        repositories = {
            repository.name: repository
            for repository in client.list_repositories(config.organisation)
        }
        pending_by_email = {
            invitation.email.casefold(): invitation
            for invitation in client.list_pending_invitations(config.organisation)
            if invitation.email is not None
        }
        for action in plan.actions:
            if action.status != ActionStatus.SUCCEEDED:
                continue
            error: str | None = None
            if action.action_type == ActionType.CREATE_TEAM:
                if action.team_name not in teams:
                    error = "created team was not present during verification"
            elif action.action_type == ActionType.CREATE_REPOSITORY:
                repository = (
                    repositories.get(action.repository) if action.repository is not None else None
                )
                if repository is None:
                    error = "created repository was not present during verification"
                elif not repository.is_private:
                    error = "created repository was not private during verification"
            elif action.action_type in {
                ActionType.GRANT_TEAM_REPOSITORY,
                ActionType.UPDATE_TEAM_REPOSITORY_PERMISSION,
            }:
                if action.team_slug is None or action.repository is None:
                    error = "permission action lacks a resolved team or repository"
                else:
                    permission = client.get_team_repository_permission(
                        config.organisation,
                        action.team_slug,
                        action.repository,
                    )
                    if permission != config.repositories.permission.value:
                        error = (
                            "verified repository permission is "
                            f"{permission!r}, expected "
                            f"{config.repositories.permission.value!r}"
                        )
            elif action.action_type == ActionType.ADD_TEAM_MEMBER:
                if action.team_slug is None or action.github_user_id is None:
                    error = "membership action lacks a resolved team or numeric GitHub user ID"
                else:
                    members = client.list_team_members(
                        config.organisation,
                        action.team_slug,
                    )
                    if not any(member.id == action.github_user_id for member in members):
                        error = (
                            "resolved GitHub user ID was not active in the "
                            "destination team during verification"
                        )
            elif action.action_type == ActionType.ARCHIVE_REPOSITORY:
                repository = (
                    repositories.get(action.repository) if action.repository is not None else None
                )
                if repository is None or not repository.is_archived:
                    error = "repository was not archived during verification"
            elif action.action_type == ActionType.REMOVE_TEAM_REPOSITORY:
                if action.team_slug is None or action.repository is None:
                    error = "removal action lacks a resolved team or repository"
                else:
                    permission = client.get_team_repository_permission(
                        config.organisation,
                        action.team_slug,
                        action.repository,
                    )
                    if permission is not None:
                        error = "team repository relationship still exists during verification"
            elif action.action_type == ActionType.SEND_INVITATION and action.email is not None:
                pending_invitation = pending_by_email.get(action.email.casefold())
                if pending_invitation is None:
                    plan.warnings.append(
                        f"Invitation for {action.email} was no longer pending during "
                        "verification; it may already have been accepted."
                    )
                else:
                    expected_team_ids = {
                        target.team_id
                        for target in _action_invitation_targets(action)
                        if target.team_id is not None
                    }
                    actual_team_ids = client.list_invitation_team_ids(
                        config.organisation,
                        pending_invitation.id,
                    )
                    if not expected_team_ids.issubset(actual_team_ids):
                        error = "pending invitation is missing expected team IDs: " + ", ".join(
                            str(team_id) for team_id in sorted(expected_team_ids - actual_team_ids)
                        )

            if error is not None:
                verification_errors.append(
                    Action(
                        action_id=f"verify:{action.action_id}",
                        action_type=ActionType.ERROR,
                        scope=action.scope,
                        student_id=action.student_id,
                        email=action.email,
                        github_login=action.github_login,
                        github_user_id=action.github_user_id,
                        group_id=action.group_id,
                        team_name=action.team_name,
                        team_slug=action.team_slug,
                        team_id=action.team_id,
                        identity_team_name=action.identity_team_name,
                        identity_team_slug=action.identity_team_slug,
                        identity_team_id=action.identity_team_id,
                        repository=action.repository,
                        reason=error,
                        status=ActionStatus.FAILED,
                    )
                )
    except GitHubRateLimitError as exc:
        verification_errors.append(
            Action(
                action_id="verify:rate-limit",
                action_type=ActionType.ERROR,
                scope="verification",
                reason=f"post-apply verification was rate limited: {exc}",
                status=ActionStatus.FAILED,
            )
        )
        exit_code = EXIT_RATE_LIMIT
    except GitHubAuthError as exc:
        verification_errors.append(
            Action(
                action_id="verify:authorisation",
                action_type=ActionType.ERROR,
                scope="verification",
                reason=f"post-apply verification was not authorised: {exc}",
                status=ActionStatus.FAILED,
            )
        )
        exit_code = EXIT_AUTH
    except GitHubError as exc:
        verification_errors.append(
            Action(
                action_id="verify:github",
                action_type=ActionType.ERROR,
                scope="verification",
                reason=f"post-apply verification failed: {exc}",
                status=ActionStatus.FAILED,
            )
        )
        exit_code = EXIT_PARTIAL
    except Exception as exc:
        verification_errors.append(
            Action(
                action_id="verify:unexpected",
                action_type=ActionType.ERROR,
                scope="verification",
                reason=f"post-apply verification failed unexpectedly: {exc}",
                status=ActionStatus.FAILED,
            )
        )
        exit_code = EXIT_PARTIAL if outcome.successful_writes else EXIT_UNEXPECTED
    else:
        exit_code = EXIT_PARTIAL if verification_errors else outcome.exit_code

    plan.actions.extend(verification_errors)
    return ExecutionOutcome(
        plan=plan,
        exit_code=exit_code,
        successful_writes=outcome.successful_writes,
        failed_writes=outcome.failed_writes + len(verification_errors),
        blocked_writes=outcome.blocked_writes,
    )


_ACTION_LABELS: dict[ActionType, str] = {
    ActionType.CREATE_TEAM: "Create teams",
    ActionType.CREATE_REPOSITORY: "Create repositories",
    ActionType.GRANT_TEAM_REPOSITORY: "Grant repository access",
    ActionType.UPDATE_TEAM_REPOSITORY_PERMISSION: "Update repository permissions",
    ActionType.ADD_TEAM_MEMBER: "Add team members",
    ActionType.SEND_INVITATION: "Send invitations",
    ActionType.SKIP_PENDING_INVITATION: "Pending invitations skipped",
    ActionType.SKIP_ACCEPTED: "Accepted or inferred skipped",
    ActionType.SKIP_UNCHANGED: "Unchanged skipped",
    ActionType.ARCHIVE_REPOSITORY: "Archive repositories",
    ActionType.REMOVE_TEAM_REPOSITORY: "Remove team access",
    ActionType.REVIEW_REQUIRED: "Review required",
    ActionType.ERROR: "Errors",
}


def action_counts(plan: Plan) -> dict[ActionType, int]:
    counts = Counter(action.action_type for action in plan.actions)
    return {action_type: counts.get(action_type, 0) for action_type in ActionType}


def terminal_summary_lines(plan: Plan) -> list[str]:
    counts = action_counts(plan)
    summary_types = [
        ActionType.CREATE_TEAM,
        ActionType.CREATE_REPOSITORY,
        ActionType.GRANT_TEAM_REPOSITORY,
        ActionType.UPDATE_TEAM_REPOSITORY_PERMISSION,
        ActionType.ADD_TEAM_MEMBER,
        ActionType.SEND_INVITATION,
        ActionType.SKIP_PENDING_INVITATION,
        ActionType.SKIP_ACCEPTED,
        ActionType.REVIEW_REQUIRED,
        ActionType.ARCHIVE_REPOSITORY,
        ActionType.REMOVE_TEAM_REPOSITORY,
        ActionType.ERROR,
    ]
    return [
        f"{_ACTION_LABELS[action_type]}: {counts[action_type]}"
        for action_type in summary_types
        if counts[action_type]
    ]


def _markdown_text(value: object) -> str:
    return (
        str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    )


def _markdown_code(value: object) -> str:
    text = _markdown_text(value)
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", text)),
        default=0,
    )
    delimiter = "`" * (longest_run + 1)
    return f"{delimiter}{text}{delimiter}"


def format_duration(seconds: float) -> str:
    total = max(0, math.ceil(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {remaining_seconds}s"
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"


def _action_detail(action: Action) -> list[str]:
    lines = [f"### {_ACTION_LABELS[action.action_type]} — {_markdown_code(action.action_id)}", ""]
    fields: list[tuple[str, object | None]] = [
        ("Status", action.status.value),
        ("Student ID", action.student_id),
        ("Email", action.email),
        ("GitHub login", action.github_login),
        ("GitHub user ID", action.github_user_id),
        ("Group", action.group_id),
        ("Team", action.team_name),
        ("Team slug", action.team_slug),
        ("Team ID", action.team_id),
        ("Identity team", action.identity_team_name),
        ("Identity team slug", action.identity_team_slug),
        ("Identity team ID", action.identity_team_id),
        ("Repository", action.repository),
        ("Invitation state", action.invitation_state.value if action.invitation_state else None),
        ("Current state", action.current_state),
        ("Desired state", action.desired_state),
    ]
    for label, value in fields:
        if value is not None:
            lines.append(f"- {label}: {_markdown_code(value)}")
    if len(action.invitation_targets) > 1:
        lines.append("- Invitation teams:")
        for target in action.invitation_targets:
            target_kind = "individual" if target.individual else "shared"
            identity = (
                f"{target.team_name} (ID {target.team_id})"
                if target.team_id is not None
                else target.team_name
            )
            lines.append(
                f"  - {_markdown_code(identity)} — "
                f"{_markdown_text(target_kind)} target "
                f"({_markdown_code(target.group_id)})"
            )
    lines.append(f"- Reason: {_markdown_text(action.reason)}")
    if action.dependencies:
        dependencies = ", ".join(_markdown_code(item) for item in action.dependencies)
        lines.append(f"- Dependencies: {dependencies}")
    if action.destructive:
        lines.append("- Destructive: `yes`")
    if action.error:
        lines.append(f"- Error: {_markdown_text(action.error)}")
    lines.append("")
    return lines


def render_plan_report(plan: Plan) -> str:
    counts = action_counts(plan)
    lines = [
        f"# {plan.title}",
        "",
        f"- Organisation: {_markdown_code(plan.organisation)}",
        f"- Subject: {_markdown_code(plan.subject)}",
        f"- Term: {_markdown_code(plan.term)}",
        f"- Mode: {_markdown_code(plan.mode)}",
        f"- Generated: {_markdown_code(format_timestamp(plan.generated_at))}",
        "",
        "## Summary",
        "",
        "| Action | Count |",
        "|---|---:|",
    ]
    for action_type in ActionType:
        count = counts[action_type]
        if count:
            lines.append(f"| {_ACTION_LABELS[action_type]} | {count} |")
    if not any(counts.values()):
        lines.append("| No actions | 0 |")

    status_counts = Counter(action.status for action in plan.actions)
    lines.extend(
        [
            "",
            "## Execution status",
            "",
            "| Status | Count |",
            "|---|---:|",
        ]
    )
    for status in ActionStatus:
        if status_counts[status]:
            lines.append(f"| {status.value.capitalize()} | {status_counts[status]} |")
    if not status_counts:
        lines.append("| No actions | 0 |")

    if plan.execution_estimate is not None:
        estimate = plan.execution_estimate
        lines.extend(
            [
                "",
                "## Execution pacing",
                "",
                f"- Planned GitHub writes: {estimate.planned_writes}",
                f"- Planned invitations: {estimate.planned_invitations}",
                f"- Invitation budget per 24 hours: {estimate.invitation_budget}",
                f"- Hourly content windows: {estimate.content_windows}",
                f"- Invitation windows: {estimate.invitation_windows}",
                f"- Estimated minimum apply time: "
                f"{_markdown_code(format_duration(estimate.minimum_seconds))}",
            ]
        )
        if plan.execution_metrics is not None:
            metrics = plan.execution_metrics
            lines.extend(
                [
                    f"- One-second pacing wait: "
                    f"{_markdown_code(format_duration(metrics.pacing_wait_seconds))}",
                    f"- Limit wait: {_markdown_code(format_duration(metrics.limit_wait_seconds))}",
                    f"- Rate-limit retries: {metrics.rate_limit_retries}",
                ]
            )
            if metrics.next_eligible_at is not None:
                lines.append(
                    f"- Next eligible write: "
                    f"{_markdown_code(format_timestamp(metrics.next_eligible_at))}"
                )

    if plan.warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {_markdown_text(warning)}" for warning in plan.warnings)

    sections: list[tuple[str, Callable[[Action], bool]]] = [
        (
            "Planned changes",
            lambda action: action.status == ActionStatus.PLANNED,
        ),
        (
            "Completed changes",
            lambda action: action.status == ActionStatus.SUCCEEDED,
        ),
        (
            "Skipped",
            lambda action: action.status == ActionStatus.SKIPPED,
        ),
        (
            "Review required",
            lambda action: (
                action.status == ActionStatus.REVIEW
                or action.action_type == ActionType.REVIEW_REQUIRED
            ),
        ),
        (
            "Errors and blocked changes",
            lambda action: (
                action.status in {ActionStatus.FAILED, ActionStatus.BLOCKED}
                or action.action_type == ActionType.ERROR
            ),
        ),
    ]
    for heading, predicate in sections:
        selected = [action for action in plan.actions if predicate(action)]
        if not selected:
            continue
        lines.extend(["", f"## {heading}", ""])
        for action in selected:
            lines.extend(_action_detail(action))
    return "\n".join(lines).rstrip() + "\n"


def render_validation_report(
    config: Configuration,
    roster: Roster,
    groups: Sequence[DesiredGroup],
    *,
    mode: RosterMode = RosterMode.GROUPS,
    generated_at: datetime | None = None,
) -> str:
    now = generated_at or utc_now()
    resource_label = "Project groups" if mode == RosterMode.GROUPS else "Individual teams"
    resource_heading = "Group" if mode == RosterMode.GROUPS else "Individual"
    lines = [
        "# GitHub Roster Validation",
        "",
        f"- Organisation: {_markdown_code(config.organisation)}",
        f"- Subject: {_markdown_code(config.subject)}",
        f"- Term: {_markdown_code(config.term)}",
        f"- Mode: {_markdown_code(mode.value)}",
        f"- Roster: {_markdown_code(roster.source)}",
        f"- Generated: {_markdown_code(format_timestamp(now))}",
        "",
        "## Summary",
        "",
        "| Item | Count |",
        "|---|---:|",
        f"| Valid students | {len(roster.students)} |",
        f"| {resource_label} | {len(groups)} |",
        f"| Expected teams | {len(groups)} |",
        f"| Expected repositories | {sum(len(group.repositories) for group in groups)} |",
        "",
        "## Expected resources",
        "",
    ]
    for group in groups:
        lines.extend(
            [
                f"### {resource_heading} {_markdown_code(group.group_id)}",
                "",
                f"- Team: {_markdown_code(group.team_name)}",
                f"- Students: {len(group.students)}",
                "- Repositories:",
            ]
        )
        lines.extend(f"  - {_markdown_code(repository.name)}" for repository in group.repositories)
        lines.append("")
    lines.extend(
        [
            "## Result",
            "",
            "The configuration, roster rows, identities, and generated resource "
            "names are structurally valid.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def write_markdown_report(
    directory: Path,
    *,
    kind: str,
    content: str,
    generated_at: datetime | None = None,
) -> Path:
    now = generated_at or utc_now()
    timestamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H%M%S.%fZ")
    safe_kind = re.sub(r"[^a-z0-9-]+", "-", kind.casefold()).strip("-")
    path = directory / f"{timestamp}_{safe_kind}.md"
    counter = 1
    while path.exists():
        path = directory / f"{timestamp}_{safe_kind}-{counter}.md"
        counter += 1
    _write_text_atomic(path, content)
    return path


def write_plan_report(
    config_path: Path,
    config: Configuration,
    plan: Plan,
    *,
    kind: str,
) -> Path:
    return write_markdown_report(
        reports_path(config_path, config),
        kind=kind,
        content=render_plan_report(plan),
        generated_at=plan.generated_at,
    )


def write_roster_validation_report(
    config_path: Path,
    config: Configuration,
    roster: Roster,
    groups: Sequence[DesiredGroup],
    *,
    mode: RosterMode = RosterMode.GROUPS,
    generated_at: datetime | None = None,
) -> Path:
    now = generated_at or utc_now()
    return write_markdown_report(
        reports_path(config_path, config),
        kind="roster-validation",
        content=render_validation_report(
            config,
            roster,
            groups,
            mode=mode,
            generated_at=now,
        ),
        generated_at=now,
    )
