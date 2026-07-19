"""Focused regression coverage for the configurable agent step ceiling."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_step_ceiling_is_1000_across_backend_and_ui():
    auth_routes = (ROOT / "routes" / "auth_routes.py").read_text(encoding="utf-8")
    chat_routes = (ROOT / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    settings_js = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")

    assert '"agent_max_rounds": (1, 1000)' in auth_routes
    assert "_max_rounds = max(1, min(_max_rounds, 1000))" in chat_routes
    assert "clampInt(roundsInput.value, 1, 1000, 20)" in settings_js
