from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gh_edu import core
from gh_edu.core import (
    EXIT_PARTIAL,
    Action,
    ActionStatus,
    ActionType,
    InputValidationError,
    InvitationLedger,
    InvitationState,
    Plan,
    build_group_resources,
    build_provision_plan,
    discover_snapshot,
    execute_plan,
    ledger_path,
    load_configuration,
    load_ledger,
    load_roster,
    render_plan_report,
    save_ledger_atomic,
    write_markdown_report,
)


def test_ledger_write_uses_same_directory_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_factory,
) -> None:
    path = tmp_path / "nested" / "ledger.json"
    ledger = InvitationLedger(
        organisation="teaching-org",
        records=[record_factory()],
    )
    actual_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def recording_replace(source, destination) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        replacements.append((source_path, destination_path))
        assert source_path.parent == destination_path.parent
        actual_replace(source, destination)

    monkeypatch.setattr(core.os, "replace", recording_replace)

    save_ledger_atomic(path, ledger)

    assert len(replacements) == 1
    assert replacements[0][1] == path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["records"][0]["status"] == "pending"
    assert not list(path.parent.glob("*.tmp"))


def test_atomic_replace_failure_preserves_existing_ledger_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_factory,
) -> None:
    path = tmp_path / "ledger.json"
    original = b'{"sentinel": true}\n'
    path.write_bytes(original)
    ledger = InvitationLedger(
        organisation="teaching-org",
        records=[record_factory()],
    )

    def fail_replace(_source, _destination) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(core.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        save_ledger_atomic(path, ledger)

    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not-json", "corrupted"),
        (
            json.dumps(
                {
                    "schema_version": 2,
                    "organisation": "teaching-org",
                    "records": [],
                }
            ),
            "schema_version",
        ),
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "organisation": "other-org",
                    "records": [],
                }
            ),
            "belongs to organisation",
        ),
    ],
)
def test_invalid_ledger_is_rejected(content, message, tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(InputValidationError, match=message):
        load_ledger(path, "teaching-org")


def test_duplicate_case_insensitive_ledger_records_are_rejected(
    tmp_path: Path,
    record_factory,
) -> None:
    path = tmp_path / "ledger.json"
    records = [
        record_factory(email="Student@Example.edu.au"),
        record_factory(email="student@example.edu.au", invitation_id=4000),
    ]
    payload = InvitationLedger(
        organisation="teaching-org",
        records=records,
    ).model_dump(mode="json")
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InputValidationError, match="duplicate records"):
        load_ledger(path, "teaching-org")


def test_ledger_failure_after_remote_invite_blocks_later_invitations(
    config_factory,
    roster_factory,
    fake_client,
    fixed_now,
    monkeypatch: pytest.MonkeyPatch,
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
    config = load_configuration(config_path)
    roster = load_roster(roster_path, config)
    groups = build_group_resources(config, roster)
    for group in groups:
        team = fake_client.add_team(group.team_name)
        repository = fake_client.add_repository(group.repositories[0].name)
        fake_client.permissions[(team.slug, repository.name)] = "push"
    ledger = InvitationLedger(organisation=config.organisation)
    snapshot = discover_snapshot(fake_client, config, groups, ledger)
    plan = build_provision_plan(
        config,
        groups,
        snapshot,
        mode="Apply",
        generated_at=fixed_now,
    )

    def fail_save(_path, _ledger) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(core, "save_ledger_atomic", fail_save)
    fake_client.clear_calls()
    outcome = execute_plan(
        plan,
        client=fake_client,
        config=config,
        ledger=ledger,
        ledger_file=ledger_path(config_path, config),
        now=lambda: fixed_now,
    )

    invitation_actions = [
        action for action in outcome.plan.actions if action.student_id is not None
    ]
    invitation_calls = [
        call for call in fake_client.write_calls if call.operation == "invite_member"
    ]
    assert outcome.exit_code == EXIT_PARTIAL
    assert len(invitation_calls) == 1
    assert invitation_actions[0].status == ActionStatus.FAILED
    assert "may exist remotely" in (invitation_actions[0].error or "")
    assert invitation_actions[1].status == ActionStatus.BLOCKED


def test_markdown_report_contains_summary_details_and_australian_spelling(
    fixed_now,
) -> None:
    plan = Plan(
        title="GitHub Provisioning Plan",
        organisation="teaching-org",
        subject="COMP3018",
        term="2026S2",
        mode="Dry run",
        generated_at=fixed_now,
        actions=[
            Action(
                action_id="group:G01:invitation:12345678",
                action_type=ActionType.SEND_INVITATION,
                scope="group:G01",
                student_id="12345678",
                email="12345678@student.example.edu.au",
                group_id="G01",
                team_name="COMP3018-2026S2-G01",
                team_id=42,
                invitation_state=InvitationState.NOT_INVITED,
                reason="no pending invitation or prior ledger record exists",
            ),
            Action(
                action_id="group:G02:invitation:87654321",
                action_type=ActionType.REVIEW_REQUIRED,
                scope="group:G02",
                student_id="87654321",
                email="87654321@student.example.edu.au",
                group_id="G02",
                team_name="COMP3018-2026S2-G02",
                invitation_state=InvitationState.UNRESOLVED,
                reason="acceptance cannot be conclusively mapped",
                status=ActionStatus.REVIEW,
            ),
        ],
    )

    markdown = render_plan_report(plan)

    assert markdown.startswith("# GitHub Provisioning Plan\n")
    assert "- Organisation: `teaching-org`" in markdown
    assert "- Mode: `Dry run`" in markdown
    assert "| Send invitations | 1 |" in markdown
    assert "| Review required | 1 |" in markdown
    assert "## Planned changes" in markdown
    assert "## Review required" in markdown
    assert "- Student ID: `12345678`" in markdown
    assert "- Invitation state: `unresolved`" in markdown


def test_report_writer_uses_markdown_files_without_overwriting(
    tmp_path: Path,
    fixed_now,
) -> None:
    first = write_markdown_report(
        tmp_path,
        kind="Provision Plan",
        content="# First\n",
        generated_at=fixed_now,
    )
    second = write_markdown_report(
        tmp_path,
        kind="Provision Plan",
        content="# Second\n",
        generated_at=fixed_now,
    )

    assert first.suffix == second.suffix == ".md"
    assert first != second
    assert first.read_text(encoding="utf-8") == "# First\n"
    assert second.read_text(encoding="utf-8") == "# Second\n"
    assert second.stem.endswith("-1")
