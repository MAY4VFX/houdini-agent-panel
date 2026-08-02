# fxhoudinimcp 2.10.0 — справочник по API для переиспользования

Источник: пакет установлен в
`/private/tmp/claude-501/.../scratchpad/venv/lib/python3.14/site-packages/fxhoudinimcp/`
(pip install fxhoudinimcp==2.10.0). Все пути ниже — относительно корня пакета
`fxhoudinimcp/`, если не указано иначе. Строки указаны по состоянию файлов на
момент чтения.

`Requires-Python: >=3.10` (METADATA). Классификаторы: 3.10, 3.11, 3.12.
`Requires-Dist: httpx>=0.27.0`, `mcp<3,>=1.14.0`, `pydantic>=2.0.0`.
Плагин внутри Houdini кладёт `uiready.py` в 4 версии Python-либ:
`python3.9libs/`, `python3.10libs/`, `python3.11libs/`, `python3.13libs/` —
т.е. поддерживает диапазон Houdini от Python 3.9 (H19.5) до 3.13 (H21+),
**включая 3.11** (H20.5, наша целевая версия).

---

## 1. `install.py` — публичные функции

### Константа
```python
SERVER_NAME = "fxhoudini"   # install.py:54 — имя, под которым сервер
                              # регистрируется у MCP-клиента
```

### `client_command() -> list[str]`  (install.py:57-66)
```python
def client_command() -> list[str]:
    return [sys.executable, "-m", "fxhoudinimcp"]
```
Всегда `sys.executable` (абсолютный путь), никогда голого `python` — потому
что Claude Desktop/Code запускает MCP-серверы без окружения пользователя, и
голое имя может резолвиться не в тот интерпретатор.

### `desktop_config_path() -> Path | None`  (install.py:69-85)
```python
def desktop_config_path() -> Path | None:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA")
        if not base:
            return None
        return Path(base) / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return (Path.home() / "Library" / "Application Support"
                 / "Claude" / "claude_desktop_config.json")
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
```
Возвращает путь **независимо от того, существует ли файл**. На macOS это
`~/Library/Application Support/Claude/claude_desktop_config.json` — не
относится напрямую к houdini-agent-panel (это про Claude Desktop, не про
Houdini), но полезно как референс формата.

### `claude_code_available() -> bool`  (install.py:88-89)
`shutil.which("claude") is not None`.

### `claude_code_add_argv(scope: str = "user") -> list[str]`  (install.py:92-103)
```python
return ["claude", "mcp", "add", "--scope", scope, SERVER_NAME,
        "--", *client_command()]
```
Т.е. фактическая команда регистрации:
`claude mcp add --scope user fxhoudini -- <sys.executable> -m fxhoudinimcp`.

### `claude_code_remove_argv(scope: str = "user") -> list[str]`  (install.py:106-113)
```python
return ["claude", "mcp", "remove", SERVER_NAME, "-s", scope]
```

### `printable_argv(argv: list[str]) -> str`  (install.py:116-118)
```python
def printable_argv(argv: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in argv)
```
Просто джойнит argv в строку, оборачивая в кавычки части с пробелами
(например путь `~/Library/.../python` без пробелов останется
без кавычек). Никакой полноценной shell-эскейпинг-логики нет — только для
показа человеку.

### `resolve_houdini_dirs(explicit: str | None) -> tuple[list[Path], str]`  (install.py:121-152)
```python
def resolve_houdini_dirs(explicit):
    if explicit:
        return [Path(explicit).expanduser()], "given on the command line"
    candidates = candidate_package_dirs()   # из houdini_package.py
    if not candidates:
        return [], "no Houdini packages directory exists yet"
    if len(candidates) == 1:
        return candidates, "the only candidate on this machine"
    return candidates, f"every candidate on this machine ({len(candidates)})"
```
Возвращает **список** директорий (может быть несколько версий Houdini на
машине) + причину для лога. Пустой список означает "нет ни одной
Houdini-preferences директории с `packages/`". Ничего не создаёт директорий
сама — только смотрит, что уже есть на диске.

### Прочие функции install.py (для полноты)
- `_merge_desktop_config(existing: dict, command: list[str]) -> dict` (155-175) —
  мержит `mcpServers.fxhoudini.{command,args}` в существующий JSON конфиг
  Claude Desktop, не трогая остальные ключи (важно: чужой `env` с
  `HOUDINI_HOST`/`HOUDINI_PORT` сохраняется).
- `pinned_port_warning(entry) -> list[str]` (178-195) — предупреждает, если в
  конфиге зафиксирован `HOUDINI_PORT`, что отключает автоскан портов.
- `install_desktop(config, command, dry_run) -> list[str]` (198-244) — пишет
  файл конфига Claude Desktop, делает `.bak` бэкап перед перезаписью.
- `claude_code_current_command() -> str | None` (247-266) — парсит вывод
  `claude mcp get fxhoudini`, ищет строку `Command:`.
- `repoint_claude_code() -> list[str]` (304-345) — делает
  `claude mcp remove` + `claude mcp add`, т.к. `claude mcp add` не умеет
  обновлять существующую запись.
- `install_claude_code(dry_run) -> list[str]` (348-379) — вызывает
  `claude_code_add_argv()` через `subprocess.run`.
- CLI: `build_parser()` (381-416) с флагами `--houdini-dir`, `--client
  {auto,claude-code,claude-desktop,both,none}`, `--client-only`, `--dry-run`.
  `main(argv)` (419-465) вызывает `_install_plugin_half` (пишет
  `fxhoudinimcp.json` во все директории из `resolve_houdini_dirs`) и
  `_install_client_half` (регистрирует в MCP-клиенте согласно `--client`).

**Важно для houdini-agent-panel**: install.py целиком заточен под
Claude Code/Claude Desktop как клиентов. Наша панель — свой собственный ACP
клиент, так что прямое переиспользование `install_*` функций не подходит
(они пишут `claude_desktop_config.json` или зовут `claude mcp add`). Что
переиспользуемо — это идея и код `resolve_houdini_dirs`/`desktop_config_path`
как паттерн, и функции из `houdini_package.py` (см. ниже) — они не привязаны
к конкретному клиенту.

---

## 2. `houdini_package.py` — как формируется package json плагина

```python
PACKAGE_NAME = "fxhoudinimcp.json"                      # :31
CLI = "python -m fxhoudinimcp"                          # :38
```

### `plugin_path() -> Path`  (:41-54)
```python
def plugin_path() -> Path:
    here = Path(__file__).resolve().parent
    packaged = here / "houdini"
    if packaged.is_dir():
        return packaged
    return here.parents[1] / "houdini"
```
В установленном wheel-пакете плагин лежит по адресу
`<site-packages>/fxhoudinimcp/houdini/` (что и подтверждено в этом
инстансе — там реально есть `houdini/`).

### `package_json(path: Path | None = None) -> str`  (:57-64)
```python
def package_json(path=None) -> str:
    target = (path or plugin_path()).as_posix()
    return json.dumps(
        {"env": [{"FXHOUDINIMCP": target}], "path": "$FXHOUDINIMCP"},
        indent=4,
    ) + "\n"
```
Возвращает JSON-строку package-файла Houdini. Прямые слэши на любой ОС.

Фактический эталонный файл, зашитый в дистрибутив плагина
(`houdini/fxhoudinimcp.json`, шаблон, не генерируемый на лету — используется
как образец/дефолт внутри самого плагина):
```json
{
    "env": [
        {"FXHOUDINIMCP": "/absolute/path/to/fxhoudinimcp/houdini"},
        {"FXHOUDINIMCP_PORT": "8100"},
        {"FXHOUDINIMCP_BIND": "127.0.0.1"},
        {"FXHOUDINIMCP_AUTOSTART": "1"},
        {"FXHOUDINIMCP_AUTO_LAYOUT": "1"}
    ],
    "path": "$FXHOUDINIMCP"
}
```
Это отличается от того, что реально пишет `write_package()` — та пишет
только `{"env": [{"FXHOUDINIMCP": <path>}], "path": "$FXHOUDINIMCP"}` (без
остальных четырёх переменных). Остальные `FXHOUDINIMCP_*` переменные,
видимо, читаются с дефолтами прямо в коде (`os.environ.get(..., "8100")` и
т.п.), а этот файл — просто образец/документация формата package-файла для
тех, кто хочет их явно прописать.

### `candidate_package_dirs() -> list[Path]`  (:67-100)
```python
def candidate_package_dirs() -> list[Path]:
    home = Path.home()
    roots: list[Path] = []
    system = platform.system()
    if system == "Windows":
        roots += [home / "Documents", home / "OneDrive" / "Documents", home]
    elif system == "Darwin":
        roots += [home / "Library" / "Preferences" / "houdini"]
    else:
        roots += [home]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.glob("houdini*")):
            packages = entry / "packages"
            if entry.is_dir() and packages.is_dir():
                found.append(packages)
    return found
```
На macOS ищет `~/Library/Preferences/houdini/houdini*/packages` — совпадает
с тем, что уже задокументировано в CLAUDE.md проекта
(`~/Library/Preferences/houdini/20.5/`). Возвращает только уже
**существующие** директории `packages/` — ничего не создаёт.

### `existing_packages(exclude=None) -> list[tuple[Path, str]]`  (:118-160)
Ищет уже записанные `fxhoudinimcp.json` в кандидатных директориях (кроме
исключённых), парсит их `env[].FXHOUDINIMCP`, возвращает список
`(путь_к_файлу, куда_он_указывает)`. Нужно для варнинга "несколько
package-файлов, последний побеждает".

### `write_package(destination: Path, path: Path | None = None) -> Path`  (:163-178)
```python
def write_package(destination: Path, path: Path | None = None) -> Path:
    if not destination.is_dir():
        raise NotADirectoryError(destination)
    target = destination / PACKAGE_NAME
    target.write_text(package_json(path), encoding="utf-8", newline="\n")
    return target
```
Пишет **без BOM** (`encoding="utf-8"`, не `utf-8-sig`) — комментарий явно
говорит, что BOM ломает JSON-парсер Houdini и файл молча игнорируется
(issue #11 в их репо).

`main(argv)` (:181-263) — CLI `fxhoudinimcp houdini-package [--write DIR]
[--path-only]`, тонкая обвязка вокруг вышеперечисленного.

**Вывод для houdini-agent-panel**: если панель ставит СВОЙ Houdini-плагин
(а не переиспользует чужой из fxhoudinimcp), то `candidate_package_dirs()`,
`write_package()`/`package_json()` — то, что стоит скопировать 1:1 как
паттерн (те же грабли: BOM, несколько packages-директорий, "не гадать
директорию"). Сами функции завязаны на `plugin_path()` этого конкретного
пакета, так что copy-paste логики, а не импорт модуля.

---

## 3. `server.py` + `bridge.py` — выбор порта и обнаружение живого сервера

### Переменные окружения (клиентская сторона — процесс MCP-сервера,
запущенного `python -m fxhoudinimcp`)
```python
host = os.getenv("HOUDINI_HOST", "localhost")   # server.py:57
pinned = os.getenv("HOUDINI_PORT")              # server.py:58
port = int(pinned) if pinned else 8100          # server.py:59
```
Если `HOUDINI_PORT` **не задан явно**, клиент сканирует диапазон портов:
```python
if not pinned:
    servers = await find_servers(host, port)    # server.py:66, base=8100
    if servers:
        port = servers[0]["port"]                # берёт первый (наименьший) живой
```
`find_servers` (bridge.py:39-67):
```python
PORT_SEARCH_RANGE = 16   # bridge.py:36 — т.е. диапазон 8100..8115 включительно

async def find_servers(host, base, max_tries=PORT_SEARCH_RANGE, timeout=1.0):
    found = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for port in range(base, base + max_tries):
            try:
                response = await client.post(
                    f"http://{host}:{port}/api",
                    data=_rpc_body("mcp.health"),
                )
                response.raise_for_status()
                payload = response.json()
            except Exception:
                continue
            if isinstance(payload, dict) and payload.get("status") == "ok":
                found.append({**payload, "port": port})
    return found
```
Т.е. **никакого файла или другого API для обнаружения порта нет** — чистое
последовательное HTTP-прощупывание `POST http://<host>:<port>/api` с телом
`mcp.health` для каждого порта в диапазоне base..base+15. Если несколько
Houdini-сессий отвечают, используется первая (наименьший порт), остальные
логируются как варнинг (server.py:69-78). Единственный способ
"узнать из процесса Houdini, на каком порту реально поднялся сервер" —
это тоже HTTP-прощупывание того же самого `mcp.health`; внутри Houdini
это делает `fxhoudinimcp_server.startup.get_port()` (см. раздел 5) — но это
внутрипроцессная переменная плагина, снаружи она недоступна иначе как через
HTTP.

Никаких файлов с портом на диске не пишется (ни в `$TMPDIR`, ни в
preferences) — обнаружение исключительно по HTTP-скану.

### Переменные окружения — сторона плагина внутри Houdini (startup.py)
```python
base = port or int(os.environ.get("FXHOUDINIMCP_PORT", "8100"))   # startup.py:175
```
```python
address = os.environ.get("FXHOUDINIMCP_BIND", "127.0.0.1")        # startup.py:128
```
```python
if os.environ.get("FXHOUDINIMCP_AUTOSTART", "1") == "1":          # uiready.py:12
```
```python
value = hou.getenv("FXHOUDINIMCP_AUTO_LAYOUT")                    # config.py (houdini-side) :22
if value is None:
    value = os.environ.get("FXHOUDINIMCP_AUTO_LAYOUT", "1")
```

Полный список env-переменных пакета:
| Переменная | Где читается | Дефолт | Смысл |
|---|---|---|---|
| `HOUDINI_HOST` | server.py:57 (клиент MCP) | `localhost` | к какому хосту стучаться |
| `HOUDINI_PORT` | server.py:58 (клиент MCP) | нет (тогда автоскан) | пин конкретного порта, **отключает автоскан** |
| `FXHOUDINIMCP_PORT` | startup.py:175 (плагин в Houdini) | `8100` | база для выбора свободного порта |
| `FXHOUDINIMCP_BIND` | startup.py:128 (плагин) | `127.0.0.1` | адрес привязки hwebserver (только loopback по умолчанию — сознательно, т.к. эндпоинт исполняет произвольный Python без аутентификации) |
| `FXHOUDINIMCP_AUTOSTART` | uiready.py:12 (плагин) | `1` | автостарт сервера при готовности UI Houdini |
| `FXHOUDINIMCP_AUTO_LAYOUT` | config.py:14-25 (оба, клиент и плагин) | `1` | разрешить тулам авто-раскладку нод в network editor |
| `MCP_TRANSPORT` | __main__.py:135 (клиент) | `stdio` | транспорт MCP-сервера |
| `LOG_LEVEL` | __main__.py:124 (клиент) | `INFO` | уровень логирования |

Если реальный порт Houdini-сессии отличается от базового (например,
вторая открытая Houdini заняла 8100 и первая уехала на 8101), плагин
логирует это в консоль Houdini (startup.py:180-184) и в UI-пункте меню
"Connect a Client..." (MainMenuCommon.xml:85-90); клиент со своей стороны
находит это автоматически сканом, если `HOUDINI_PORT` не пинить.

---

## 4. `bridge.py` — протокол HTTP и как стартует MCP-сервер для клиента

Houdini's `hwebserver` — RPC-стиль:
```
POST /api
Content-Type: application/x-www-form-urlencoded
Body: json=["namespace.function", [positional_args], {keyword_args}]
```
(bridge.py:1-10). Реализовано через `_rpc_body(func_name, **kwargs)`
(bridge.py:29-31), которая заворачивает в `{"json": json.dumps([func_name, [], kwargs])}`.

`HoudiniBridge.execute(command, params, timeout)` (bridge.py:123-207) шлёт
`mcp.execute` с `{"command": ..., "params": ..., "request_id": uuid4()}`,
разбирает `status: success|error` в ответе, конвертирует ошибки Houdini в
`HoudiniCommandError`/`ConnectionError` (errors.py). Есть retry на
`httpx.RemoteProtocolError` (пересоздаёт connection pool) — актуально после
рестарта Houdini (bridge.py:98-121).

`HoudiniBridge.health_check()` (bridge.py:209-232) → `mcp.health`.
`HoudiniBridge.list_commands()` (bridge.py:234-252) → `mcp.list_commands`.

### Команда запуска MCP-сервера для клиента (точный argv)

Из `install.py`:
```python
command = "auto"           # sys.executable, абсолютный путь к Python
args = ["-m", "fxhoudinimcp"]
```
т.е. итоговый объект для `mcpServers`:
```json
{
  "fxhoudini": {
    "command": "/absolute/path/to/python",
    "args": ["-m", "fxhoudinimcp"]
  }
}
```
(Это ровно то, что пишет `_merge_desktop_config()`, install.py:168-175, и что
шлёт `claude mcp add` через `claude_code_add_argv()`, install.py:92-103.)
`env` не задаётся автоматически install.py — если его не задать явно,
процесс наследует окружение родителя (клиента), и `HOUDINI_HOST`/`HOUDINI_PORT`
берут дефолты `localhost`/автоскан 8100-8115. Явно указать порт/хост можно,
добавив в `env` вручную:
```json
{
  "fxhoudini": {
    "command": "/absolute/path/to/python",
    "args": ["-m", "fxhoudinimcp"],
    "env": {"HOUDINI_HOST": "localhost", "HOUDINI_PORT": "8101"}
  }
}
```
Install.py предупреждает (`pinned_port_warning`, install.py:178-195), что
задание `HOUDINI_PORT` отключает автоскан для *других* Houdini-сессий.

**Для houdini-agent-panel**: панель должна собрать `mcpServers[0]` ровно в
этой форме — `{name: "fxhoudini", command: <python>, args: ["-m",
"fxhoudinimcp"], env: {...опционально...}}`. `<python>` — это путь к
интерпретатору, в котором **установлен пакет fxhoudinimcp** (не обязательно
`sys.executable` самой панели — Houdini обычно свой Python, а fxhoudinimcp
ставится либо в системный/venv-Python пользователя, вызывающий его
отдельно как MCP stdio сервер, либо через `pip install` в тот же Python,
которым запущена панель, если она это делает сама. Нужно проверить, куда
именно панель ставит зависимости — это вне текста этого файла).

---

## 5. `node_versions.py` — ВАЖНО: это НЕ про Node.js!

Файл `node_versions.py` не имеет отношения к JS-рантайму Node или его
установке/скачиванию. Название происходит от **Houdini node types**
(типы нод в сети, например `colorcorrect`, `layout`) — модуль отслеживает,
**в каких версиях Houdini какие ноды существовали**, чтобы предупреждать,
если инструкции для LLM устарели.

```python
_TABLE = Path(__file__).parent / "data" / "sampled_versions.json"   # :29

@lru_cache(maxsize=1)
def load_table() -> dict: ...        # читает JSON {"builds": {...}, "series": [...]}

def series_of(version: str | None) -> str | None: ...   # "22.0.368" -> "22.0"

def sampled_series() -> list[str]: ...   # список минорных серий Houdini,
                                          # для которых есть данные, отсортированный

def staleness_warning(version: str | None) -> str | None:
    # None если версия покрыта данными; иначе строка-предупреждение
    # "старше/новее чем всё, что есть в таблице сэмплов"
```
Никакой логики поиска/скачивания настоящего Node.js (JavaScript runtime)
здесь нет и во всём пакете фраза "download node" не встречается ни в одном
файле — только "node types" в контексте Houdini SOP/LOP/etc нод. Если
команде нужна логика поиска/скачивания Node.js для панели (например, если
ACP-агент — это Node-процесс), в этом пакете такой функциональности **нет**,
искать в другом месте.

---

## 6. `_loader.py`, `houdini/` — как плагин стартует внутри Houdini

### `_loader.py` (верхнеуровневый, для MCP-сервера-клиента, не для плагина)
```python
_MD_DIR = Path(__file__).parent / "prompts" / "markdown"

@cache
def _read(name: str) -> str: ...      # читает markdown-файл раз, кэширует

def load_markdown(name: str, **kwargs: str) -> str: ...
```
Загружает и кэширует markdown-файлы инструкций/промптов
(`prompts/markdown/instructions/…`, `workflows/…`, `shared/…`). Не имеет
отношения к запуску сервера внутри Houdini — это для MCP `instructions=` и
`@mcp.prompt()`.

### Структура `houdini/` (плагин, ставится в Houdini packages)
```
houdini/
├── fxhoudinimcp.json           # шаблон package-файла (см. раздел 2)
├── MainMenuCommon.xml           # пункты меню MCP > Start/Stop/Connect/Status
├── python3.9libs/uiready.py     # идентичный код для каждой версии Python Houdini
├── python3.10libs/uiready.py
├── python3.11libs/uiready.py    # ← актуальна для Houdini 20.5
├── python3.13libs/uiready.py
└── scripts/python/fxhoudinimcp_server/
    ├── __init__.py
    ├── startup.py                # старт/стоп hwebserver, выбор порта, readiness poll
    ├── config.py                 # auto_layout_enabled() через hou.getenv
    ├── dispatcher.py             # роутинг command -> handler
    ├── errors.py
    ├── serialize.py              # json_default для несериализуемых HOM-объектов
    ├── outputs.py
    ├── ui.py
    ├── hwebserver_app.py          # регистрация HTTP endpoints (mcp.execute/health/...)
    └── handlers/*.py              # ~20 файлов, реальная логика по категориям
```

### Автостарт: `uiready.py` (идентичен во всех `python*libs/`)
```python
# fxhoudinimcp/houdini/python3.11libs/uiready.py — целиком:
import os

if os.environ.get("FXHOUDINIMCP_AUTOSTART", "1") == "1":
    try:
        import fxhoudinimcp_server.startup
        fxhoudinimcp_server.startup.ensure_running(wait=False)
    except Exception as e:
        print(f"[fxhoudinimcp] Auto-start failed: {e}")
```
`uiready.py` — специальный файл, который Houdini сама подхватывает и
исполняет **один раз после инициализации UI** (комментарий в файле
уточняет: "в отличие от `scripts/456.py`, это корректно стекуется с другими
пакетами, тоже определяющими `uiready.py`" — т.е. это Houdini-нативный
механизм, не самописный). `wait=False` — readiness-poll идёт в отдельном
потоке, не блокируя UI Houdini.

### `startup.py` — жизненный цикл сервера (весь модуль ключевой)
- `_pick_free_port(base, probe=None, my_pid=None, max_tries=16)` (81-113):
  идёт от `base` вверх, пропускает порты, отвечающие как **чужой** pid;
  порт, отвечающий как **свой** pid, возвращает как есть (идемпотентность
  рестарта); порт без ответа — свободен.
- `_bind_localhost_only(hwebserver)` (116-137): жёстко ограничивает бинд на
  `127.0.0.1` (или `FXHOUDINIMCP_BIND`) **до** запуска —
  `hwebserver.setSettingsForPort({"ADDRESS": address}, "main")` (порядок
  аргументов важен: сначала dict, потом имя порта "main").
- `start(port=None, background=None, wait=True)` (140-239): импортирует
  `hou`, `hwebserver`, регистрирует хендлеры через
  `from fxhoudinimcp_server import handlers, hwebserver_app`, вызывает
  `hwebserver.run(_port, debug=False, in_background=background)`.
  `background` по умолчанию = `hou.isUIAvailable()`.
- `ensure_running(wait=True)` (317-332): идемпотентный старт, используется и
  автостартом (`wait=False`), и пунктом меню "Start Server" (`wait=True`,
  неявно через `mcp.start()` в MainMenuCommon.xml).
- `get_port() -> int` (307-309), `is_running() -> bool` (302-304),
  `is_starting() -> bool` (312-314) — публичный статус, используется и меню
  "Server Status...", и мог бы использоваться панелью, **но только изнутри
  процесса Houdini** (через `mcp__fxhoudini__execute_python` или
  `hou.session`, не снаружи).

### `hwebserver_app.py` — регистрация HTTP endpoints
```python
@_api_function("mcp")
def execute(request, command="", params=None, request_id=""):
    result = dispatcher.dispatch(command, params)
    result["request_id"] = request_id
    return _json_response(result)

@_api_function("mcp")
def health(request):
    return {"status": "ok", "pid": os.getpid(),
            "houdini_version": os.environ.get("HOUDINI_VERSION", "unknown")}

@_api_function("mcp")
def session_info(request):
    return _json_response(dispatcher.dispatch("scene.get_scene_info", {}))

@_api_function("mcp")
def list_commands(request):
    return {"commands": dispatcher.list_commands()}
```
`health` **намеренно не трогает `hou.*`** (комментарий: main thread может
быть занят readiness-loop'ом самого стартапа — обращение к HOM с воркер-
потока в этот момент задедлочит процесс). Поэтому `health` не содержит
`hip_file` — за ним нужен отдельный вызов `scene.get_scene_info` (через
`session_info` endpoint или через обычный `mcp.execute`).

---

## 7. `compat.py`, `config.py` — конфиг и совместимость (НЕ пользовательский data dir)

**У пакета нет пользовательской config/data директории** (никакого
`platformdirs`/`appdirs`, никакого `~/.fxhoudinimcp/` и т.п. не найдено).
Единственные "конфиги" на диске:
1. Houdini package file `fxhoudinimcp.json` в `<houdini-prefs>/packages/`
   (раздел 2/3) — путь к плагину + опционально доп. env-переменные.
2. Конфиг клиента (`claude_desktop_config.json` и т.п.) — принадлежит
   MCP-клиенту, не самому fxhoudinimcp.

### `compat.py` (верхнеуровневый — сторона MCP-клиента)
Сверяет список команд, которые реально зарегистрировал плагин
(`bridge.list_commands()`), со списком команд, которые нужны серверу
(`data/required_commands.json`, сгенерирован из мест вызова `execute()`):
```python
def missing_commands(available: list[str] | None) -> list[str]:
    required = required_commands()
    if not required or not available:
        return []
    ...
    return sorted(required - set(available))

def compatibility_warning(available) -> str | None:
    # None если ничего не пропущено, иначе текст с именами команд
```
Используется в `server.py` lifespan (сервер предупреждает при коннекте,
если плагин старее сервера) и в `tools/scene.py:get_houdini_connection_status`.

### `config.py` (верхнеуровневый, клиентская сторона)
```python
_FALSY = {"0", "false", "off", "no"}

def auto_layout_enabled() -> bool:
    value = os.getenv("FXHOUDINIMCP_AUTO_LAYOUT", "1")
    return value.strip().lower() not in _FALSY
```
Дублируется (с поправкой на `hou.getenv`) в
`houdini/scripts/python/fxhoudinimcp_server/config.py` — на стороне плагина.

---

## 8. Проверка живости: health-эндпоинт

`mcp.health` (hwebserver_app.py:97-115):
```json
{"status": "ok", "pid": 12345, "houdini_version": "20.5.584"}
```
**НЕ содержит `hip_file`** — намеренно (см. раздел 6, комментарий про
main-thread deadlock). Для `hip_file` нужен отдельный запрос —
готовый пример есть в `tools/scene.py:get_houdini_connection_status`
(строки 20-95, MCP tool верхнего уровня, не эндпоинт плагина):
```python
health = await bridge.health_check()          # mcp.health, без hip_file
...
if "hip_file" not in health:
    scene = await bridge.execute("scene.get_scene_info", timeout=5.0)
    for key in ("hip_file", "houdini_version"):
        if scene.get(key) is not None:
            health.setdefault(key, scene[key])
```
`scene.get_scene_info` реализован в
`houdini/scripts/python/fxhoudinimcp_server/handlers/scene_handlers.py:70,89`:
```python
hip_path = hou.hipFile.path()
...
"hip_file": hip_path,
```
Это ровно то, что упомянуто в `CLAUDE.md` проекта houdini-agent-panel:
"`get_houdini_connection_status` у fx отвечает `connected: true`" — это MCP
tool `fxhoudinimcp/tools/scene.py`, доступный уже установленному в этой
сессии MCP-серверу `mcp__fxhoudini__get_houdini_connection_status`.

`houdini_version` берётся из переменной окружения `HOUDINI_VERSION`,
которую сама Houdini экспортирует — не вычисляется кодом плагина
(hwebserver_app.py:114).

---

## Резюме (что переиспользовать в houdini-agent-panel)

1. **mcpServers-запись для агента** — точная форма:
   `{"fxhoudini": {"command": "<python с установленным fxhoudinimcp>",
   "args": ["-m", "fxhoudinimcp"], "env": {опционально HOUDINI_HOST/PORT}}}`
   (install.py:57-66, 92-103, 168-175).
2. **Обнаружение живого Houdini** — только HTTP-скан `POST /api` с
   `mcp.health` по портам 8100..8115 (bridge.py:36,39-67; server.py:57-80).
   Никакого файла/API для "узнать порт снаружи" не существует, кроме этого
   скана. Диапазон и таймаут (`1.0s` на порт) — единственные параметры.
3. **Health/liveness** — `mcp.health` даёт `status/pid/houdini_version`, БЕЗ
   `hip_file`; за `hip_file` нужен `scene.get_scene_info` отдельным вызовом
   (см. `get_houdini_connection_status` как готовый паттерн, tools/scene.py:20-95).
4. **Установка Houdini package-файла** — паттерн из `houdini_package.py`
   (кандидатные директории только macOS `~/Library/Preferences/houdini/
   houdini*/packages`, писать без BOM, писать во все кандидаты, не гадать)
   переиспользуем как *логику*, не как импортируемый код (завязан на
   `plugin_path()` этого пакета).
5. **`node_versions.py` не про Node.js** — если панели нужен поиск/установка
   Node.js рантайма, этой функциональности в fxhoudinimcp нет вообще.
6. **Автостарт плагина** — Houdini-нативный `uiready.py` (не наш код,
   Houdini сама его подхватывает после инициализации UI) — если панель
   ставит СВОЙ Houdini-плагин, это готовый to-copy паттерн для автостарта
   без блокировки UI (поток + `wait=False`).
7. **Нет пользовательского config/data dir** в пакете — если панели нужно
   хранить свои настройки, ориентир из fxhoudinimcp — писать конфиг рядом
   с MCP-клиентом (Desktop config) или использовать Houdini package env,
   но не файл в домашней директории пользователя.
