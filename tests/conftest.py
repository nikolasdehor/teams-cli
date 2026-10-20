import pytest


@pytest.fixture(autouse=True)
def _no_real_xteams_tokens(monkeypatch, tmp_path):
    """Os testes nunca podem ler o cache de tokens real do xteams."""
    monkeypatch.setenv("TEAMS_XTEAMS_CACHE", str(tmp_path / "sem-cache-xteams.json"))
