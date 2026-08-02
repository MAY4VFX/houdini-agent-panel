"""Тесты AuthView: экран входа рисуется целиком из authMethods."""

from __future__ import annotations

from PySide6 import QtWidgets

from houdini_agent_panel.client import AuthMethod
from houdini_agent_panel.ui.auth_view import AuthView


def _method_buttons(view: AuthView) -> list[QtWidgets.QPushButton]:
    """Кнопки способов входа — только из `_methods_layout`, не из всего дерева:
    `_logout_button` тоже `QPushButton` и парентится раньше динамических
    кнопок, так что порядок в `findChildren` на него полагаться не может."""
    return [view._methods_layout.itemAt(i).widget() for i in range(view._methods_layout.count())]


def test_renders_one_button_per_auth_method(qapp):
    view = AuthView()
    view.show()
    methods = [
        AuthMethod(id="anthropic", name="Anthropic Console", description="OAuth"),
        AuthMethod(id="api-key", name="API-ключ"),
    ]
    view.set_methods(methods, can_logout=False)

    assert [b.text() for b in _method_buttons(view)] == ["Anthropic Console", "API-ключ"]


def test_method_button_click_emits_method_chosen(qapp):
    view = AuthView()
    view.set_methods([AuthMethod(id="anthropic", name="Anthropic Console")], can_logout=False)

    received = []
    view.method_chosen.connect(received.append)
    _method_buttons(view)[0].click()
    assert received == ["anthropic"]


def test_logout_button_hidden_without_capability(qapp):
    view = AuthView()
    view.set_methods([AuthMethod(id="a", name="A")], can_logout=False)
    assert not view._logout_button.isVisible()


def test_logout_button_visible_with_capability_and_emits_signal(qapp):
    view = AuthView()
    view.show()
    view.set_methods([AuthMethod(id="a", name="A")], can_logout=True)
    assert view._logout_button.isVisible()

    received = []
    view.logout_requested.connect(lambda: received.append(True))
    view._logout_button.click()
    assert received == [True]


def test_empty_methods_shows_placeholder_not_blank_screen(qapp):
    view = AuthView()
    view.show()
    view.set_methods([], can_logout=False)
    assert view._empty_label.isVisible()


def test_set_methods_replaces_previous_buttons(qapp):
    view = AuthView()
    view.set_methods([AuthMethod(id="a", name="A")], can_logout=False)
    view.set_methods([AuthMethod(id="b", name="B")], can_logout=False)
    assert [b.text() for b in _method_buttons(view)] == ["B"]


def test_no_own_fields_beyond_what_agent_sent(qapp):
    """Нет своих полей логина/пароля — только то, что пришло в authMethods."""
    view = AuthView()
    view.set_methods([AuthMethod(id="a", name="A")], can_logout=False)
    assert view.findChildren(QtWidgets.QLineEdit) == []
