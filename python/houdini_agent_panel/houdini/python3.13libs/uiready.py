"""Автозагрузочный файл Houdini — выполняется один раз после готовности UI.

Houdini сама подхватывает `uiready.py` из любой директории `python3.11libs`,
входящей в HOUDINI_PATH (включая пути, добавленные пакетами), и вызывает её
после инициализации UI — это штатный, а не самописный механизм (см.
docs/facts/houdini.md §4). Панель поднимается из `.pypanel`, когда художник
сам открывает таб, а не отсюда: автостарт агента внутри панели — вопрос
настроек панели, а не загрузки Houdini.

Единственная задача этого файла — не молчать, если дерево зависимостей ещё не
поставлено ($HAP_DEPS пуст, не существует или установка пакета прервалась на
середине). Пустая панель без единого слова в консоли — худшее, что можно
показать художнику, когда что-то не так. Поэтому импортов нашего пакета на
уровне модуля здесь нет: если сама установка сломана, эта диагностика обязана
отработать в любом случае, а не упасть на первой же строке.
"""

import os


def _check() -> None:
    deps = os.environ.get("HAP_DEPS")
    if not deps:
        print(
            "[houdini_agent_panel] переменная HAP_DEPS не задана — package json "
            "панели не подхватился Houdini."
        )
        return

    package_dir = os.path.join(deps, "houdini_agent_panel")
    if not os.path.isdir(package_dir):
        print(
            f"[houdini_agent_panel] зависимости не найдены в {deps}. "
            "Похоже, установка не завершена. Открой консоль/терминал и запусти "
            "`python -m houdini_agent_panel doctor` для диагностики."
        )
        return

    acp_dir = os.path.join(deps, "acp")
    if not os.path.isdir(acp_dir):
        print(
            f"[houdini_agent_panel] пакет acp (ACP SDK) не найден в {deps}. "
            "Установка зависимостей прервалась на середине — запусти "
            "`python -m houdini_agent_panel doctor`."
        )


try:
    _check()
except Exception as exc:  # noqa: BLE001 - Houdini ронять нельзя ни при каких обстоятельствах
    print(f"[houdini_agent_panel] uiready.py упал: {exc!r}")
