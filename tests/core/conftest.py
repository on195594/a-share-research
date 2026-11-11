from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_external_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the default unit-test suite offline and detached from production credentials."""
    for name in (
        "TUSHARE_TOKEN",
        "TG_TOKEN",
        "TG_CHAT_ID",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
