"""settings.py: round-tripping `config_options_by_agent` (the artist's last
pick per agent — model, effort — remembered across a Houdini restart, see
`ui/panel.py::AgentPanel._reapply_remembered_config`)."""

from __future__ import annotations

from houdini_agent_panel import settings as settings_module
from houdini_agent_panel.settings import Settings


def test_config_options_by_agent_defaults_to_empty():
    assert Settings().config_options_by_agent == {}


def test_config_options_by_agent_round_trips(tmp_path):
    path = tmp_path / "settings.json"
    current = Settings()
    current.config_options_by_agent["claude-acp"] = {"model": "sonnet", "effort": "high"}
    settings_module.save(current, path)

    reloaded = settings_module.load(path)
    assert reloaded.config_options_by_agent == {"claude-acp": {"model": "sonnet", "effort": "high"}}


def test_config_options_by_agent_ignores_malformed_entries(tmp_path):
    """Hand-edited settings.json shouldn't crash the panel — same tolerance
    as every other field (`Settings.from_dict`'s own docstring)."""
    path = tmp_path / "settings.json"
    path.write_text(
        '{"config_options_by_agent": {"claude-acp": "not-a-dict", "codex-acp": {"model": 5}}}',
        "utf-8",
    )
    reloaded = settings_module.load(path)
    assert reloaded.config_options_by_agent == {"codex-acp": {"model": "5"}}
