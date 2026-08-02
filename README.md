# houdini-agent-panel

Панель-чат с AI-агентом внутри SideFX Houdini. Ставится одной командой, агент
поднимается сам — ни терминала, ни портов, ни правки конфигов.

> Статус: v1 в сборке. Дизайн — [`docs/design.md`](docs/design.md), контракт модулей —
> [`docs/architecture.md`](docs/architecture.md), проверенные факты о внешних API —
> [`docs/facts/`](docs/facts/).

## Зачем

Чтобы сегодня пользоваться агентом со сценой Houdini, нужен открытый терминал с `claude`
и настройка MCP руками. Художник этого не сделает.

Панель — надстройка над [fxhoudinimcp](https://github.com/healkeiser/fxhoudinimcp)
(189 инструментов работы со сценой поверх официального `hwebserver` Houdini). Мы не
пишем ни агента, ни инструменты — только ACP-клиент и инсталлятор, связывающие готовое.

## Агенты

Из официального [реестра ACP](https://github.com/agentclientprotocol/registry):
Claude Agent, Codex, Gemini CLI, Grok Build, Kimi CLI, OpenCode — плюс «Свой агент»
для всего остального. Ставится только выбранный, а не все сразу. Node, если нужен,
панель приносит с собой.

Свои и удалённые модели подключаются как модель внутри OpenCode: ACP работает только
через stdio, поэтому агент всегда локальный, а эндпоинт модели может быть где угодно.

## Установка

Одной командой, одинаково на macOS, Linux и Windows:

```bash
uvx --from houdini-agent-panel python -m houdini_agent_panel install --agents opencode
```

Она находит все установленные Houdini, ставит панель в Python каждой из них,
прописывает пакет и качает выбранного агента. В систему ничего не попадает:
`uvx` запускает инсталлятор и уходит.

Нет `uv`? Он ставится тоже одной командой и не требует прав root:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # macOS, Linux
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows
```

В репозитории лежат `scripts/install.sh` и `scripts/install.ps1` — они делают
оба шага сразу и пригодятся, когда репозиторий станет публичным и их можно
будет вызывать через `curl … | sh`.

Перезапустить Houdini → панель появится в меню панелей (Tab → Python Panels → Agent).

Инсталлятор находит на машине все установленные Houdini и для каждой ставит панель
**в её собственный Python**. Это не прихоть: `pydantic` тащит скомпилированное ядро, а у
Houdini 20.5 внутри Python 3.11 и у Houdini 22 — 3.13, так что одно общее дерево
зависимостей на обе версии физически невозможно. Подробности — в
[`docs/architecture.md`](docs/architecture.md) §0.

Полезное:

```bash
python -m houdini_agent_panel install --dry-run   # показать план, ничего не менять
python -m houdini_agent_panel doctor              # что нашлось и что сломано
python -m houdini_agent_panel uninstall --purge   # убрать вместе с папкой данных
```

Ставится из локально собранного колеса — `--find-links dist`. Полностью офлайн —
добавить `--offline` и положить в ту же папку колёса всех зависимостей.

## Приватность

Телеметрия выключена по умолчанию и включается только явно. Что собирается и что не
собирается никогда — [`docs/privacy.md`](docs/privacy.md).

## Разработка

```bash
uv venv --python 3.11 .venv            # 3.11 — нижняя поддерживаемая версия (Houdini 20.5)
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q
```

Тесты не ходят в сеть и не пишут за пределы `tmp_path` — это обеспечено автофикстурами
в `tests/conftest.py`, а не дисциплиной.

UI можно разрабатывать без запуска Houdini. Preview использует настоящие Qt-виджеты
панели, fake session и автоматически перезапускается после сохранения Python-файлов:

```bash
.venv/bin/python -m houdini_agent_panel.dev_preview --watch
```

В окне можно раскрывать custom dropdowns/tool rows, отвечать на permission-card и
отправлять сообщения: fake-turn покажет spinner, buddy action и завершение `Worked for…`.

## Лицензия

MIT
