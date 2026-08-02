#!/bin/sh
# Установка панели агента в Houdini — одной командой.
#
#   curl -fsSL <адрес>/install.sh | sh
#   curl -fsSL <адрес>/install.sh | sh -s -- --agents opencode
#
# Скрипт сознательно на /bin/sh, а не на bash: на минимальных образах Linux и
# в некоторых студийных окружениях bash может отсутствовать, а сообщение
# «bash: not found» в ответ на «поставь мне панель» — худшее из первых
# впечатлений.
#
# Что делает: находит способ запустить пакет с PyPI и передаёт ему установку.
# Сам ничего в систему не кладёт, кроме uv — и то лишь если запускать нечем.
set -eu

PACKAGE="houdini-agent-panel"

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }

has() { command -v "$1" >/dev/null 2>&1; }

# uvx и pipx запускают пакет, не устанавливая его в систему — для инсталлятора
# это ровно то, что нужно: он отработал и ушёл, не оставив за собой ни venv,
# ни записей в системном Python.
if has uvx; then
    say "Ставлю через uvx…"
    exec uvx --from "$PACKAGE" python -m houdini_agent_panel install "$@"
fi

if has pipx; then
    say "Ставлю через pipx…"
    exec pipx run --spec "$PACKAGE" python -m houdini_agent_panel install "$@"
fi

# Ни того ни другого нет. Приносим uv — это один статический бинарь в
# домашней папке пользователя, без прав root и без пакетного менеджера
# системы. Тот же приём, что и у самой Houdini, приносящей свой Python.
if has curl; then
    say "Не нашёл uvx и pipx, приношу uv…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
elif has wget; then
    say "Не нашёл uvx и pipx, приношу uv…"
    wget -qO- https://astral.sh/uv/install.sh | sh
else
    die "Нужен curl или wget, чтобы что-то скачать. Поставь любой из них и повтори."
fi

# Установщик uv кладёт бинарь сюда и просит перезайти в шелл; нам перезаходить
# некуда, поэтому находим его сами.
for candidate in "${XDG_BIN_HOME:-}/uvx" "$HOME/.local/bin/uvx" "$HOME/.cargo/bin/uvx"; do
    if [ -x "$candidate" ]; then
        say "Ставлю через $candidate…"
        exec "$candidate" --from "$PACKAGE" python -m houdini_agent_panel install "$@"
    fi
done

die "uv установился, но uvx не нашёлся. Открой новый терминал и выполни:
    uvx --from $PACKAGE python -m houdini_agent_panel install"
