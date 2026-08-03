from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTHENTICATION_DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "docs" / "user-guide.md",
)
FRESH_LOGIN = (
    "gh auth login --hostname github.com --web "
    "--scopes admin:org,read:org,repo"
)
REFRESH_LOGIN = "gh auth refresh --hostname github.com --scopes admin:org"
AUTH_STATUS = "gh auth status --active --hostname github.com"
APP_CHECK = "gh-edu auth check --config config.yml"


def _compact(value: str) -> str:
    return " ".join(value.split())


def test_authentication_documentation_uses_one_supported_contract() -> None:
    for path in AUTHENTICATION_DOCUMENTS:
        content = path.read_text(encoding="utf-8")
        compact = _compact(content)

        assert FRESH_LOGIN in compact
        assert REFRESH_LOGIN in compact
        assert AUTH_STATUS in compact
        assert APP_CHECK in compact
        assert not re.search(r"(?m)^\s*gh auth login\s*$", content)


def test_authentication_documentation_covers_scopes_and_token_safety() -> None:
    for path in AUTHENTICATION_DOCUMENTS:
        content = path.read_text(encoding="utf-8")
        compact = _compact(content)

        for scope in ("admin:org", "read:org", "repo"):
            assert scope in content
        assert "Do not use `gh auth status --show-token`" in compact
        assert "owner" in content.casefold()
