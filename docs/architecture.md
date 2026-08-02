# Архитектура — контракт модулей

Обязательное чтение перед правкой кода. Здесь зафиксированы **точные** публичные
API каждого модуля: сигнатуры, dataclass'ы, Qt-сигналы. Модули пишутся разными
людьми параллельно и стыкуются только по этому документу — менять сигнатуру
можно, но тогда правится и он, и все вызывающие.

Проверенные факты о внешних API — в [`facts/`](facts/): [ACP SDK](facts/acp-sdk.md),
[fxhoudinimcp](facts/fxhoudinimcp.md), [Houdini](facts/houdini.md). Продуктовые
решения — в [`design.md`](design.md).

---

## 0. Как это вообще запускается

Три разных Python участвуют в жизни панели, и путать их — главный источник багов:

| Кто | Что это | Что в нём стоит |
|---|---|---|
| **installer python** | тот, в котором сделали `pip install houdini-agent-panel` | CLI инсталлятора, `fxhoudinimcp` |
| **Houdini python** | `$HFS/bin/hython`, 3.11 у H20.5, 3.13 у H22 | панель и её зависимости — в отдельном `--target`-дереве |
| **agent process** | Node/бинарь агента | ничего нашего |

`pydantic` тащит скомпилированный `pydantic_core`, поэтому положить site-packages
installer-питона на `PYTHONPATH` Houdini нельзя: у 3.11 и 3.13 разные ABI-теги, и
`import pydantic` упадёт. Поэтому инсталлятор ставит панель **в саму Houdini**:

```
python -m houdini_agent_panel install
  ├─ находит папки packages каждой Houdini на машине
  ├─ для каждой находит её hython и его версию (3.11 / 3.13)
  ├─ hython -m pip install --target <data>/deps/py3.11 houdini-agent-panel==<версия>
  └─ пишет <prefs>/packages/houdini_agent_panel.json
```

Package json (единственное место, где склеиваются пути):

```json
{
    "env": [
        { "HAP_DEPS": "/Users/x/Library/Application Support/HoudiniAgentPanel/deps/py3.11" },
        { "HAP_PYTHON": "/opt/homebrew/bin/python3.12" },
        { "PYTHONPATH": { "value": "$HAP_DEPS", "method": "prepend" } }
    ],
    "path": "$HAP_DEPS/houdini_agent_panel/houdini"
}
```

- `PYTHONPATH` **prepend**, не append: дерево панели должно выигрывать у всего,
  что пользователь уже насыпал в окружение.
- `HAP_PYTHON` — installer python. Панели он нужен ровно для одного: собрать
  `mcpServers[0].command`, потому что `fxhoudinimcp` как MCP-сервер живёт именно
  там (см. §4).
- `path` даёт Houdini дерево плагина: `python3.11libs/`, `python3.13libs/`,
  `python_panels/`.

Требует сети на установку. Оффлайн — `--find-links DIR`, тогда pip берёт колёса
оттуда.

---

## 1. `paths.py` — где что лежит

Своей зависимости на `platformdirs` не берём: одна функция на три ОС дешевле
лишнего колеса в `--target`-дереве.

```python
APP_NAME = "HoudiniAgentPanel"

def data_dir() -> Path
    """Корень пользовательских данных. Создаётся при первом обращении.

    macOS   ~/Library/Application Support/HoudiniAgentPanel
    Windows %LOCALAPPDATA%/HoudiniAgentPanel
    Linux   $XDG_DATA_HOME/houdini-agent-panel (или ~/.local/share/...)

    Переопределяется переменной HAP_DATA_DIR — это же точка входа для тестов.
    """

def deps_dir(python_tag: str | None = None) -> Path   # <data>/deps/py3.11
def agents_dir() -> Path                              # <data>/agents
def agent_dir(agent_id: str) -> Path                  # <data>/agents/<id>
def node_dir() -> Path                                # <data>/node
def cache_dir() -> Path                               # <data>/cache
def logs_dir() -> Path                                # <data>/logs
def settings_path() -> Path                           # <data>/settings.json
def python_tag(version_info=None) -> str              # "py3.11"
def open_in_file_manager(path: Path) -> None          # кнопка «Открыть»
```

---

## 2. `settings.py` — настройки панели

Один JSON, читается целиком, пишется атомарно (`.tmp` + `os.replace`). Никаких
частичных мержей: файл маленький, а атомарная замена спасает от обрезанного
файла при падении Houdini.

```python
@dataclass
class CustomAgent:
    id: str
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

@dataclass
class InstalledAgent:
    agent_id: str
    version: str
    kind: str            # "npx" | "binary" | "custom"
    installed_at: str    # ISO 8601 UTC

@dataclass
class Settings:
    version: int = 1
    default_agent: str | None = None
    autostart_agent: bool = True
    check_updates: bool = True
    show_announcements: bool = True
    telemetry: bool = False
    telemetry_consent_asked: bool = False
    whisper_endpoint: str = ""
    custom_agents: list[CustomAgent] = ...
    installed_agents: dict[str, InstalledAgent] = ...
    seen_announcements: list[str] = ...

    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, payload: dict) -> "Settings"   # неизвестные ключи игнорирует,
                                                      # отсутствующие берёт из дефолтов

def load(path: Path | None = None) -> Settings
def save(settings: Settings, path: Path | None = None) -> None
def diagnostics(settings: Settings) -> str
    """Текст для кнопки «Скопировать диагностику»: версии панели/fx/Qt/Python,
    ОС, версия Houdini, id и версии установленных агентов, порт fx, источник Qt.
    Без путей к сценам, без содержимого настроек-секретов."""
```

Битый JSON — не ошибка: `load` возвращает дефолты и переименовывает файл в
`settings.json.broken`, чтобы человек не остался без панели из-за одной запятой.

---

## 3. `registry.py` — реестр ACP

```python
REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"

#: Шестёрка из design.md. Порядок — порядок показа в UI.
FEATURED_AGENT_IDS: tuple[str, ...] = (
    "claude-code-acp", "codex-acp", "gemini-cli", "grok-build", "kimi-cli", "opencode",
)

@dataclass(frozen=True)
class NpxDistribution:
    package: str              # "@zed-industries/claude-code-acp@1.2.3"
    args: list[str]

@dataclass(frozen=True)
class BinaryDistribution:
    archive: str
    cmd: str                  # "./opencode" — относительно корня распакованного архива
    args: list[str]
    sha256: str

@dataclass(frozen=True)
class AgentEntry:
    id: str
    name: str
    version: str
    description: str = ""
    repository: str = ""
    website: str = ""
    license: str = ""
    icon: str = ""
    authors: tuple[str, ...] = ()
    npx: NpxDistribution | None = None
    binaries: Mapping[str, BinaryDistribution] = ...   # ключ — platform_key()

    @property
    def needs_node(self) -> bool
    def distribution_for(self, key: str | None = None) -> NpxDistribution | BinaryDistribution | None
        """None — агента нельзя поставить на эту платформу (например Kimi на
        darwin-x86_64). UI обязан показать это причиной, а не молча спрятать."""

def platform_key() -> str
    """darwin-aarch64 | darwin-x86_64 | linux-aarch64 | linux-x86_64 | windows-x86_64"""

def parse_registry(payload: Mapping) -> list[AgentEntry]
def fetch_registry(*, force: bool = False, max_age: float = 86400.0,
                   fetch: Fetcher | None = None) -> list[AgentEntry]
    """Кеш в <cache>/registry.json. Сеть недоступна — отдаёт кеш любого возраста;
    кеша нет — RegistryError."""

class RegistryError(RuntimeError): ...
```

`Fetcher` — общий для всего проекта протокол сетевого доступа, чтобы тесты
никогда не ходили в сеть:

```python
class Fetcher(Protocol):
    def __call__(self, url: str, *, timeout: float = 30.0) -> bytes: ...

def urlopen_fetch(url: str, *, timeout: float = 30.0) -> bytes   # реализация на urllib
```

---

## 4. `scene.py` — привязка к своей сцене Houdini

Панель живёт **внутри** процесса Houdini, поэтому порт своего fx-сервера она
знает точно, не угадывая: `fxhoudinimcp_server.startup.get_port()` — та же
переменная процесса, что и у самого сервера. HTTP-скан 8100..8115 остаётся
только запасным вариантом (плагин fx не загружен / старая версия).

```python
FX_SERVER_NAME = "fxhoudini"

def fx_port() -> int | None
    """Порт fx-сервера в ЭТОМ процессе Houdini. None — сервер не поднят."""

def fx_host() -> str                       # "127.0.0.1"

def fx_python() -> str
    """Интерпретатор, в котором стоит fxhoudinimcp: $HAP_PYTHON, иначе
    sys.executable. Внутри Houdini sys.executable — бинарь Houdini, поэтому
    без HAP_PYTHON MCP-сервер не поднимется; это состояние надо показать
    человеку, а не падать."""

def mcp_servers() -> list[dict]
    """Ровно то, что уходит в session/new как mcpServers.

    [{"name": "fxhoudini",
      "command": "/opt/homebrew/bin/python3.12",
      "args": ["-m", "fxhoudinimcp"],
      "env": [{"name": "HOUDINI_HOST", "value": "127.0.0.1"},
              {"name": "HOUDINI_PORT", "value": "8101"}]}]

    Пин порта здесь обязателен: без него MCP-сервер сканирует диапазон и может
    подключиться к ЧУЖОЙ открытой Houdini. Формат env — список
    {name, value} (McpServerStdio.env: list[EnvVariable]), не словарь.
    """

def hip_dir() -> str
    """$HIP. ТОЛЬКО с главного потока. Несохранённая сцена — $HOME, а не
    несуществующий untitled-путь: cwd в session/new обязан существовать."""

def houdini_version() -> str
def is_fx_available() -> bool
```

`hou` импортируется лениво внутри функций: модуль обязан импортироваться в
тестах вне Houdini.

---

## 5. `runtime.py` + `node.py` — установка агентов

```python
# node.py
MIN_NODE = (20, 0, 0)
NODE_VERSION = "22.14.0"      # что качаем, если системного нет

def find_system_node(minimum: tuple[int, int, int] = MIN_NODE) -> Path | None
def node_platform() -> tuple[str, str]                # ("darwin", "arm64")
def dist_url(version: str = NODE_VERSION) -> str
def shasums_url(version: str = NODE_VERSION) -> str
def install_node(*, version: str = NODE_VERSION, progress: Progress | None = None,
                 fetch: Fetcher | None = None) -> Path
    """Качает архив с nodejs.org, сверяет по SHASUMS256.txt, распаковывает в
    <data>/node/<version>. Возвращает путь к бинарю node. Систему не трогает."""
def ensure_node(*, progress: Progress | None = None) -> Path
def npx_argv(node_bin: Path, package: str, args: Sequence[str]) -> list[str]
    """[<node>, <npx-cli.js>, "--yes", package, *args] — зовём npx-cli.js
    напрямую тем же node, а не шелловый шим: шим ищет node в PATH, которого у
    нас нет."""

# runtime.py
class Progress(Protocol):
    def __call__(self, done: int, total: int | None, note: str) -> None: ...

@dataclass(frozen=True)
class LaunchSpec:
    command: str
    args: list[str]
    env: dict[str, str]      # добавка к окружению, не замена

class InstallError(RuntimeError): ...
class ChecksumError(InstallError): ...

def is_installed(entry: AgentEntry) -> bool
def installed_version(agent_id: str) -> str | None
def install_agent(entry: AgentEntry, *, progress: Progress | None = None,
                  fetch: Fetcher | None = None) -> LaunchSpec
    """Бинарный — качает, сверяет sha256, распаковывает в <data>/agents/<id>/<version>,
    ставит +x. npx — ensure_node() и записывает манифест; сам пакет притащит npx
    при первом запуске. Хеш не сошёлся — ChecksumError и НИЧЕГО не остаётся на диске."""
def uninstall_agent(agent_id: str) -> None
def launch_spec(entry: AgentEntry) -> LaunchSpec
def custom_launch_spec(agent: CustomAgent) -> LaunchSpec
def download_and_verify(url, sha256, dest, *, progress=None, fetch=None) -> Path
def extract_archive(archive: Path, dest: Path) -> None
    """tar.gz/tgz/zip. Пути с .. и абсолютные — отклоняются (Zip Slip)."""
```

---

## 6. `client.py` — ACP поверх QThread

Самая рискованная часть проекта. Правила:

- asyncio-цикл живёт на своём `QThread`, `hou` из него не трогаем **никогда**;
- наружу — только Qt-сигналы (очередь Qt делает их потокобезопасными);
- внутрь — только `asyncio.run_coroutine_threadsafe`;
- `qasync` не используем.

```python
class AcpWorker(QtCore.QObject):
    """Живёт на рабочем потоке. Владеет циклом, процессом агента, соединением."""

class AcpClient(QtCore.QObject):
    """Фасад на ГЛАВНОМ потоке. Единственное, что видит UI."""

    # --- жизненный цикл соединения
    connected = Signal(object)            # AgentInfo
    disconnected = Signal(str)            # причина, "" при штатном стопе
    failed = Signal(str)                  # текст для человека
    auth_required = Signal(list)          # list[AuthMethod]
    log_line = Signal(str)                # stderr агента, для диагностики

    # --- сессии
    session_started = Signal(str, object) # session_id, SessionState
    modes_changed = Signal(str, object)   # session_id, SessionModeState
    commands_changed = Signal(str, list)  # session_id, list[AvailableCommand]

    # --- лента
    message_chunk = Signal(str, str, str) # session_id, message_id, text
    thought_chunk = Signal(str, str, str)
    tool_call = Signal(str, object)       # session_id, ToolCall
    tool_call_update = Signal(str, object)
    plan_changed = Signal(str, list)      # session_id, list[PlanEntry]
    usage_changed = Signal(str, object)   # session_id, Usage
    turn_finished = Signal(str, str)      # session_id, stop_reason
    error = Signal(str, str)              # session_id (может быть ""), текст

    # --- разрешения: запрос наружу, ответ обратно
    permission_requested = Signal(str, str, object, list)
        # request_key, session_id, ToolCallUpdate, list[PermissionOption]

    def __init__(self, parent=None) -> None
    def start(self, spec: LaunchSpec, *, cwd: str) -> None
    def stop(self) -> None
    def is_running(self) -> bool
    def agent_info(self) -> AgentInfo | None

    def authenticate(self, method_id: str) -> None
    def new_session(self, *, cwd: str, mcp_servers: list[dict]) -> None
    def prompt(self, session_id: str, blocks: list[dict]) -> None
    def cancel(self, session_id: str) -> None
    def set_mode(self, session_id: str, mode_id: str) -> None
    def answer_permission(self, request_key: str, option_id: str | None) -> None
        """option_id=None — «отменено», уходит DeniedOutcome."""
```

`AgentInfo` — плоский снимок `initialize`, чтобы UI не тянул pydantic-модели:

```python
@dataclass(frozen=True)
class AgentInfo:
    name: str
    version: str
    protocol_version: int
    supports_image: bool
    supports_audio: bool
    supports_embedded_context: bool
    supports_load_session: bool
    supports_logout: bool
    auth_methods: tuple[AuthMethod, ...]

@dataclass(frozen=True)
class AuthMethod:
    id: str
    name: str
    description: str = ""
```

**Правило UI живёт здесь**: `supports_*` — единственный источник правды о том,
рисовать ли кнопку вложений и микрофон. Панель ничего не решает сама.

Реализация опирается на `acp.spawn_agent_process` (см.
[facts/acp-sdk.md §1](facts/acp-sdk.md)). Два подводных камня оттуда, оба
обязательны:

1. `default_environment()` отдаёт агенту почти пустое окружение (`HOME`, `PATH`,
   `SHELL`, `TERM`, `USER`). Всё остальное — явным `env=`.
2. Лимит stdio-буфера по умолчанию 64 КБ. Картинка в base64 его переполнит, и
   соединение повиснет. Передаём `transport_kwargs={"limit": 50 * 1024 * 1024}`.

`stderr` агента читается отдельной задачей и уходит в `log_line` — иначе
заполнившийся пайп подвешивает процесс.

---

## 7. `sessions.py` — пул сессий на одном соединении

```python
@dataclass
class SessionState:
    session_id: str
    title: str                      # первая строка первого промпта, иначе «Новый разговор»
    cwd: str
    created_at: float
    current_mode_id: str | None = None
    available_modes: list[SessionMode] = ...
    available_commands: list[AvailableCommand] = ...
    entries: list[Entry] = ...      # лента, см. §8
    usage: Usage | None = None
    busy: bool = False

class SessionPool(QtCore.QObject):
    added = Signal(str)
    removed = Signal(str)
    changed = Signal(str)
    current_changed = Signal(str)

    def add(self, state: SessionState) -> None
    def get(self, session_id: str) -> SessionState | None
    def all(self) -> list[SessionState]
    def current(self) -> SessionState | None
    def set_current(self, session_id: str) -> None
    def remove(self, session_id: str) -> None
```

Одна `SessionPool` на процесс Houdini (модуль-синглтон `pool()`), потому что
второй таб панели обязан видеть тот же список сессий и тот же живой процесс
агента. Два таба — один `AcpClient`, один процесс, разные `current`.

---

## 8. `transcript_model.py` — модель ленты (без Qt-виджетов)

Отделено от отрисовки, чтобы логику сборки ленты можно было тестировать без
QApplication.

```python
EntryKind = Literal["user", "agent", "thought", "tool", "plan", "permission", "error"]

@dataclass
class Entry:
    kind: EntryKind
    id: str              # message_id / tool_call_id / uuid
    text: str = ""
    tool: ToolCallView | None = None
    plan: list[PlanEntry] = ...
    permission: PermissionView | None = None

@dataclass
class ToolCallView:
    tool_call_id: str
    title: str
    kind: str            # ToolKind, "other" если агент не прислал
    status: str          # pending | in_progress | completed | failed
    content: list[dict] = ...
    locations: list[dict] = ...

@dataclass
class PermissionView:
    request_key: str
    tool_title: str
    options: list[tuple[str, str, str]]   # (option_id, name, kind)
    answered: str | None = None

class TranscriptModel:
    """Складывает поток session/update в список Entry.

    Чанки с одним message_id склеиваются в одну запись — иначе лента
    превращается в сотню однобуквенных абзацев. tool_call_update находит запись
    по tool_call_id и патчит только пришедшие поля (None = «не менялось»).
    plan заменяет предыдущий план целиком (протокол шлёт полный список).
    """
    def append_user(self, text: str) -> Entry
    def apply_chunk(self, message_id: str, text: str, *, thought: bool = False) -> Entry
    def apply_tool_call(self, call) -> Entry
    def apply_tool_update(self, update) -> Entry | None
    def apply_plan(self, entries) -> Entry
    def apply_permission(self, view: PermissionView) -> Entry
    def resolve_permission(self, request_key: str, option_id: str | None) -> Entry | None
    def append_error(self, text: str) -> Entry
    def entries(self) -> list[Entry]
```

---

## 9. `updates.py`, `announcements.py`, `telemetry.py`

```python
# updates.py
PYPI_URL = "https://pypi.org/pypi/{name}/json"

@dataclass(frozen=True)
class Update:
    kind: str        # "agent" | "panel" | "fx"
    target: str      # agent_id или имя пакета
    label: str       # что показать человеку
    current: str
    latest: str

def is_newer(latest: str, current: str) -> bool
    """Сравнение по PEP 440 с откатом на посегментное сравнение чисел.
    Мусор в версии — False: молчание лучше ложной плашки."""
def pypi_latest(name: str, *, fetch: Fetcher | None = None) -> str | None
def check(*, settings: Settings, entries: list[AgentEntry],
          force: bool = False, fetch: Fetcher | None = None) -> list[Update]
    """Не чаще раза в сутки; результат и время — в <cache>/updates.json.
    settings.check_updates=False — [] и НИ ОДНОГО запроса."""

# announcements.py
FEED_URL = "https://raw.githubusercontent.com/MAY4VFX/houdini-agent-panel/main/feed/announcements.json"

@dataclass(frozen=True)
class Button:
    label: str
    url: str = ""

@dataclass(frozen=True)
class Announcement:
    id: str
    severity: str          # "info" | "blocking"
    title: str
    body: str = ""
    buttons: tuple[Button, ...] = ()
    panel_versions: str = ""    # спецификатор PEP 440, "" — всем
    expires: str = ""           # ISO 8601, "" — бессрочно

def parse_feed(payload) -> list[Announcement]
def applicable(items, *, panel_version: str, seen: Collection[str],
               now: datetime | None = None) -> list[Announcement]
def check(*, settings: Settings, panel_version: str, force: bool = False,
          fetch: Fetcher | None = None) -> list[Announcement]

# telemetry.py
def is_enabled(settings: Settings) -> bool
def build_payload(settings, *, event: str, **extra) -> dict
    """Только: версии панели/fx/агента, ОС, версия Houdini, факт падения и тип
    исключения. Никогда: пути, содержимое сцены, текст промптов, id агентских
    сессий. Проверяется тестом на запрещённые ключи."""
def send(event: str, *, settings: Settings, **extra) -> None
    """Выключена или эндпоинт не задан — no-op без единого сетевого вызова.
    Ошибки сети глушатся: телеметрия не имеет права ломать работу."""
```

Один и тот же суточный поход в сеть обслуживает и обновления, и оповещения —
`network.py::daily_refresh()`. Выключены оба тумблера — не идём никуда.

---

## 10. UI

Дерево виджетов. Каждый файл — один публичный класс; никто не лезет в приватные
атрибуты соседа, общение через сигналы.

```
AgentPanel (ui/panel.py)                 корневой QWidget, его возвращает onCreateInterface()
├── HeaderBar (ui/chips.py)              чип агента · чип $HIP · выбор сессии · «+» · шестерёнка
├── NoticeStrip (ui/announcement.py)     тихая плашка обновления/оповещения
├── QStackedWidget
│   ├── TranscriptView (ui/transcript.py)   лента
│   ├── AgentsView (ui/agents.py)           экран «Агенты»
│   ├── SettingsView (ui/settings_view.py)  экран настроек
│   └── AuthView (ui/auth_view.py)          экран входа, рисуется из authMethods
└── Composer (ui/composer.py)            ввод, «+», микрофон, чип режима, счётчик, отправка/стоп
    └── BlockingNotice (ui/announcement.py) попап НАД полем ввода
```

```python
# ui/panel.py
class AgentPanel(QtWidgets.QWidget):
    def __init__(self, parent=None) -> None
    def shutdown(self) -> None      # зовётся из onDeactivateInterface

# ui/chips.py
class HeaderBar(QtWidgets.QWidget):
    agent_clicked = Signal()
    session_selected = Signal(str)
    new_session_clicked = Signal()
    settings_clicked = Signal()
    def set_agent(self, name: str, icon: QtGui.QIcon | None) -> None
    def set_cwd(self, path: str) -> None
    def set_sessions(self, states: list[SessionState], current: str | None) -> None

class ModeChip(QtWidgets.QWidget):      # живёт в Composer
    mode_selected = Signal(str)
    def set_modes(self, modes: list[SessionMode], current_id: str | None) -> None
        """Пустой список — виджет скрывается целиком. Агент не умеет — контрола нет."""

# ui/transcript.py
class TranscriptView(QtWidgets.QScrollArea):
    permission_answered = Signal(str, str)     # request_key, option_id ("" = отменено)
    def set_model(self, model: TranscriptModel) -> None
    def refresh(self, entry_id: str | None = None) -> None
        """entry_id=None — перерисовать всё (смена сессии). Иначе — только одну
        запись: перерисовка всей ленты на каждый чанк стрима видна глазом."""

# ui/permissions.py
class PermissionRow(QtWidgets.QWidget):
    answered = Signal(str, str)
    def __init__(self, view: PermissionView, parent=None) -> None
        """Кнопки строятся строго из view.options. Порядок — как прислал агент.
        Своих кнопок не добавляем."""

# ui/composer.py
class Composer(QtWidgets.QWidget):
    submitted = Signal(list)      # list[dict] — готовые контент-блоки ACP
    cancelled = Signal()
    mode_selected = Signal(str)
    def set_capabilities(self, info: AgentInfo | None, whisper: str) -> None
    def set_busy(self, busy: bool) -> None        # кнопка отправки ↔ стоп
    def set_commands(self, commands: list[AvailableCommand]) -> None
    def set_usage(self, usage) -> None
    def block_input(self, reason: str) -> None    # блокирующее оповещение
    def unblock_input(self) -> None

# ui/agents.py
class AgentsView(QtWidgets.QWidget):
    agent_chosen = Signal(str)
    closed = Signal()

# ui/settings_view.py
class SettingsView(QtWidgets.QWidget):
    changed = Signal()
    closed = Signal()

# ui/auth_view.py
class AuthView(QtWidgets.QWidget):
    method_chosen = Signal(str)
    logout_requested = Signal()
    def set_methods(self, methods: list[AuthMethod], *, can_logout: bool) -> None

# ui/announcement.py
class NoticeStrip(QtWidgets.QWidget):
    action_clicked = Signal(str, str)   # announcement_id, url
    dismissed = Signal(str)
    def show_notice(self, ann: Announcement) -> None
    def show_update(self, update: Update) -> None

class BlockingNotice(QtWidgets.QWidget):
    action_clicked = Signal(str, str)
    def show_notice(self, ann: Announcement) -> None
```

Стиль — только через Qt-палитру Houdini (виджеты наследуют её сами) и точечные
`setStyleSheet` на своих виджетах. Глобальный стиль приложения не трогаем: это
чужое окно, в нём живёт вся остальная Houdini.

---

## 11. Тесты

`pytest`, вся сеть замокана через `Fetcher`, весь диск — через `HAP_DATA_DIR` в
`tmp_path`. Ни один тест не открывает Houdini и не ходит в интернет.

- `tests/fake_agent.py` — минимальный ACP-агент на `acp.run_agent`, отдельным
  процессом. На нём проверяется настоящий `AcpClient`: стрим, разрешения,
  режимы, `auth_required`, отмена. Это единственный честный способ протестировать
  протокольный слой, и он дешёвый.
- UI-тесты — `QApplication` из `PySide6`, `qWait` вместо `sleep`.
- `tests/test_no_network.py` — с выключенными тумблерами `Fetcher` не зовётся
  ни разу (пункт 17 из Verification в design.md).
