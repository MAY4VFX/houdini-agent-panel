"""Agent-side settings: model, reasoning effort, fast mode.

This is where ACP actually puts the model picker — agents expose it through
`configOptions` on the `session/new` reply, not as a dedicated protocol
concept. The client used to ignore both that field and the
`config_option_update` notification, so the model chip in the panel could
never show anything and stayed hidden forever.

Verified against live agents: claude-acp offers permission mode, model,
effort and fast mode; codex-acp offers approval, collaboration mode, model,
reasoning effort and fast mode.
"""

from __future__ import annotations

from houdini_agent_panel.client import ConfigChoice, ConfigOption, _config_options_from


class _Choice:
    def __init__(self, value, name, description=""):
        self.value = value
        self.name = name
        self.description = description


class _Option:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_select_option_is_flattened_for_the_ui():
    raw = [
        _Option(
            id="model",
            name="Model",
            current_value="opus[1m]",
            description="Which model answers",
            category="model_config",
            options=[
                _Choice("default", "Default (recommended)", "Best for everyday tasks"),
                _Choice("opus[1m]", "Opus (1M context)"),
            ],
        )
    ]

    parsed = _config_options_from(raw)

    assert len(parsed) == 1
    option = parsed[0]
    assert isinstance(option, ConfigOption)
    assert option.id == "model"
    assert option.current_value == "opus[1m]"
    assert option.category == "model_config"
    assert option.choices[0] == ConfigChoice(
        value="default", name="Default (recommended)", description="Best for everyday tasks"
    )


def test_option_without_choices_is_skipped():
    """Booleans and future kinds are dropped rather than guessed at: drawing
    a control we don't understand is worse than not drawing it."""
    parsed = _config_options_from([_Option(id="verbose", name="Verbose", options=None)])

    assert parsed == []


def test_missing_fields_do_not_crash_the_parse():
    """The registry and the agents are someone else's data — a missing name
    must not cost the artist the whole picker."""
    parsed = _config_options_from([_Option(id="model", options=[_Choice("a", "")])])

    assert len(parsed) == 1
    assert parsed[0].choices[0].name == "a", "a nameless choice falls back to its value"


def test_empty_input_is_empty_output():
    assert _config_options_from(None) == []
    assert _config_options_from([]) == []
