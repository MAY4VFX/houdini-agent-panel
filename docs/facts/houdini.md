# Houdini Python-панели и пакеты — проверенные факты

Собрано на месте установки: Houdini 20.5.445 и Houdini 22.0.368
(`/Applications/Houdini/`), пользовательские prefs
`~/Library/Preferences/houdini/20.5` и `~/Library/Preferences/houdini/22.0`.
Ничего не выдумано — везде указан источник (путь к файлу).

---

## 1. Формат `.pypanel`

Корневой тег — `<pythonPanelDocument>`, внутри один или несколько
`<interface>`. Комментарий в самих файлах Houdini прямо предупреждает:
"It should not be hand-edited when it is being used by the application."
(но как шаблон для генерации файла руками — нормально, Houdini просто
перезапишет его при следующем сохранении через UI).

### Пример 1 — простейший, с колбэком `onNodePathChanged`

Источник: `.../Houdini20.5.445/.../Resources/houdini/help/examples/python_panels/nodepath.pypanel`
(идентичный файл есть и в установке путём `python_panels/` для примеров;
дословно):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<pythonPanelDocument>
  <!-- This file contains definitions of Python interfaces and the
 interfaces menu.  It should not be hand-edited when it is being
 used by the application.  Note, that two definitions of the
 same interface or of the interfaces menu are not allowed
 in a single file. -->
  <interface name="NodePathExample" label="Node Path Example" icon="hicon:/SVGIcons.index?DATATYPES_node_path.svg" showNetworkNavigationBar="true" help_url="">
    <script><![CDATA[from hutil.Qt import QtWidgets

class NodePathExample(QtWidgets.QWidget):
    def __init__(self):
        super(NodePathExample, self).__init__()
        
        instruction_label = QtWidgets.QLabel(
            "Please navigate the Houdini node network using the network editor.")
            
        self.currentNodePathLabel = QtWidgets.QLabel()
        
        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(instruction_label)
        layout.addSpacing(5)
        layout.addWidget(self.currentNodePathLabel)
        layout.addStretch(1)
        
        self.setLayout(layout)
        
    def updateCurrentNodePathLabel(self, node_path):
        self.currentNodePathLabel.setText("Current Node Path: %s" % node_path)

theExampleWidget = NodePathExample()

def onCreateInterface():
    global theExampleWidget
    return theExampleWidget

def onNodePathChanged(node):
    global theExampleWidget

    if node:
        node_path = node.path()
    else:
        node_path = "None"
    theExampleWidget.updateCurrentNodePathLabel(node_path)

 ]]></script>
    <includeInToolbarMenu menu_position="102" create_separator="false"/>
    <help><![CDATA[]]></help>
  </interface>
</pythonPanelDocument>
```

### Пример 2 — реальная панель SideFX с полным набором колбэков жизненного цикла

Источник: `.../Houdini22.0.368/.../Resources/houdini/python_panels/NodeInfo.pypanel`
(дословно):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<pythonPanelDocument>
  <!-- This file contains definitions of Python interfaces and the
 interfaces menu.  It should not be hand-edited when it is being
 used by the application.  Note, that two definitions of the
 same interface or of the interfaces menu are not allowed
 in a single file. -->
    <interface name="sidefx::node_info" label="Node Info"
               icon="BUTTONS_chooser_node" showNetworkNavigationBar="true"
               help_url="/network/info">
        <script><![CDATA[
from hutil.qt.info import window

thePanel = None

def onCreateInterface():
    global thePanel
    thePanel = window.NodeInfoPanel()
    return thePanel

def onActivateInterface():
    thePanel.panelActivated()

def onDeactivateInterface():
    thePanel.panelDeactivated()

def onNodePathChanged(node):
    thePanel.nodePathChanged(node)

]]>
        </script>
        <includeInPaneTabMenu menu_position="500" create_separator="false"/>
        <help></help>
    </interface>
</pythonPanelDocument>
```

Ещё один реальный, минимальный (без node navigation, использует
`toolutils.safe_reload` для hot-reload модуля при пересоздании интерфейса) —
`.../Houdini22.0.368/.../python_panels/LogViewer.pypanel`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<pythonPanelDocument>
  <!-- ... -->
  <interface name="log_viewer" label="Log Viewer" icon="MISC_python" showNetworkNavigationBar="false" help_url="">
    <script><![CDATA[import logviewer.panel
import toolutils
toolutils.safe_reload(logviewer.panel)

def onCreateInterface():
    return logviewer.panel.LogViewerPanel()
]]></script>
    <includeInPaneTabMenu menu_position="500" create_separator="false"/>
    <help><![CDATA[]]></help>
  </interface>
</pythonPanelDocument>
```

### Атрибуты `<interface>` (наблюдаемые значения)

- `name` — уникальный ID интерфейса (строка, может содержать `::` как
  неймспейс у SideFX-панелей, например `sidefx::node_info`).
- `label` — видимое имя в меню панелей.
- `icon` — либо имя иконки Houdini (`MISC_python`, `BUTTONS_chooser_node`),
  либо `hicon:/`-путь к SVG (`hicon:/SVGIcons.index?DATATYPES_node_path.svg`).
- `showNetworkNavigationBar` — `"true"`/`"false"`: показывать ли строку
  breadcrumb-навигации по сети над панелью (не влияет напрямую на вызовы
  `onNodePathChanged`, это про UI-полосу).
- `help_url` — либо пусто, либо путь в Houdini help (`/network/info`).

### Обязательная и опциональные функции в `<script>`

- `onCreateInterface()` — **обязательна**. Возвращает `QWidget` (или
  подкласс), который Houdini встраивает в панель. Вызывается каждый раз,
  когда пользователь создаёт новый таб этой панели.
- `onNodePathChanged(node)` — опциональна, вызывается при смене "текущего
  узла" (то, что подсвечено в network editor / выбрано как контекст),
  `node` — `hou.Node` или `None`.
- `onActivateInterface()` / `onDeactivateInterface()` — опциональны,
  вызываются когда таб панели становится активным/неактивным. Это
  переключение вкладок, а НЕ закрытие: вешать на них освобождение ресурсов
  нельзя.
- `onDestroyInterface()` — опциональна, вот это и есть закрытие таба.
  Встречается в 29 штатных панелях 22.0 (`PoseLibrary.pypanel` зовёт оттуда
  `cleanup()`, `LightLinker.pypanel` обнуляет свой объект). Пересчёт по
  установке: `onCreateInterface` — 35, `onDestroyInterface` — 29,
  `onActivateInterface`/`onDeactivateInterface` — по 25, `onNodePathChanged` — 19.
- В области видимости `<script>` доступен глобальный `kwargs`, и в нём лежит
  `paneTab` — этим различаются несколько открытых табов одной панели.
  Дословно из `LightLinker.pypanel`:
  ```python
  def onActivateInterface():
      global theLightLinker
      theLightLinker.activate(kwargs.get('paneTab', None))
  ```

Меню-теги — на выбор один из:
- `<includeInToolbarMenu menu_position="N" create_separator="false"/>` —
  панель появляется в меню "Toolbar" списка панелей.
- `<includeInPaneTabMenu menu_position="N" create_separator="false"/>` —
  появляется в меню создания таба пейна (Tab menu → Python Panels и в
  контекстном меню пейна).

### Регистрация — где искать `.pypanel`

Houdini сканирует директорию `python_panels/` в каждом пути, входящем в
`HOUDINI_PATH` (включая пути, добавленные пакетами через `"path"` в JSON
пакета). Отдельного `Panels.txt`/menu-файла для python-панелей не требуется —
в отличие от shelf-тулов (`toolbar/*.shelf` + `MainMenuCommon`), каждый
`.pypanel` самодостаточен и сам себя регистрирует в общем реестре Python
Panels при старте Houdini. Подтверждено расположением: SideFX кладёт свои
панели в `$HFS/houdini/python_panels/*.pypanel`, а сторонние пакеты — в
`<package_path>/python_panels/*.pypanel`, например:
`.../Houdini22.0.368/.../Resources/packages/shotbuilder/python_panels/ShotLoad.pypanel`,
`.../packages/kinefx/python_panels/mixer.pypanel`,
`.../packages/apex/python_panels/apexselectionmanager.pypanel` — то есть
директория ищется относительно любого элемента `HOUDINI_PATH`, не только
`$HFS/houdini`.

---

## 2. Houdini package JSON

Реальные примеры из `~/Library/Preferences/houdini/{20.5,22.0}/packages/*.json`
пользователя (дословно, значения путей — реальные, с машины пользователя):

`~/Library/Preferences/houdini/22.0/packages/fxhoudinimcp.json`:
```json
{
    "env": [
        {
            "FXHOUDINIMCP": "~/.local/share/uv/tools/fxhoudinimcp/lib/python3.12/site-packages/fxhoudinimcp/houdini"
        }
    ],
    "path": "$FXHOUDINIMCP"
}
```

`~/Library/Preferences/houdini/22.0/packages/houdinimcp.json`:
```json
{
  "path": "$HOME/Library/Preferences/houdini/22.0/scripts/python/houdinimcp",
  "load_package_once": true,
  "version": "0.1",
  "env": [
    {
      "PYTHONPATH": {
        "value": "$HOME/Library/Preferences/houdini/22.0/scripts/python",
        "method": "append"
      }
    }
  ]
}
```

`~/Library/Preferences/houdini/20.5/packages/tentaculo.json`:
```json
{
  "load_package_once": true,
  "env": [
    {
      "CTENTACULO_LOCATION": "~/cerebro/ctentaculo"
    },
    {
      "HOUDINI_MENU_PATH": {
        "value": "&:~/cerebro/ctentaculo/tentaculo/api/ihoudini",
        "method": "append"
      }
    },
    {
      "PYTHONPATH": {
        "value": "~/cerebro/ctentaculo",
        "method": "append"
      }
    }
  ]
}
```

Наблюдаемые ключи и их смысл:
- `"path"` — добавляется в `HOUDINI_PATH` (сюда же попадёт `python_panels/`,
  если такая поддиректория есть внутри — см. раздел 1).
- `"env"` — список объектов `{ИМЯ_ПЕРЕМЕННОЙ: значение}`. Значение может быть
  просто строкой (перезапись/установка) либо объектом
  `{"value": ..., "method": "append"|"prepend"|"set"}` для управления тем,
  как переменная комбинируется с уже существующим значением (важно для
  `PYTHONPATH`/`HOUDINI_PATH`, чтобы не затереть системные значения).
- `"load_package_once": true` — пакет обрабатывается один раз даже если
  файл встречается по нескольким путям поиска пакетов (защита от дублей).
- `"version"` — произвольная строка версии пакета, не влияет на поведение
  загрузчика напрямую, скорее для документации/дебага.
- Переменные внутри `env`-значений разворачиваются лениво в духе `$VAR`
  (например `$FXHOUDINIMCP` использует переменную, определённую в том же
  файле чуть выше — порядок в списке `env` важен).
- `"enable"` и `"hpath"` в этих реальных файлах не встретились, но по
  документации SideFX: `"enable": false` отключает пакет без удаления
  файла, `"hpath"` — добавляет запись в `HOUDINI_PATH` так же, как
  верхнеуровневый `"path"`, но специально для случая нескольких путей.
  Раз в этих файлах их нет — не подтверждаю форму записи, только называние.
- `"recommends"` в проверенных файлах отсутствует — не подтверждено на
  этой машине.

Каталоги, где Houdini ищет `packages/*.json` (подтверждено самим наличием
файлов там): `$HOUDINI_USER_PREF_DIR/packages/` — то есть
`~/Library/Preferences/houdini/20.5/packages/` и
`~/Library/Preferences/houdini/22.0/packages/` отдельно на каждую версию.

---

## 3. `hutil.PySide`

Модуль реально существует в обеих версиях по пути
`.../Resources/houdini/python3.{11,13}libs/hutil/PySide/__init__.py`.
Полный текст (дословно, идентичен в 20.5 и 22.0):

```python
"""
hutil.PySide is a thin wrapper package that imports from PySide6 if
running in a Houdini Qt 6 build and from PySide2 otherwise.
"""

import os
import importlib
import pkgutil
import sys

if os.environ.get("__HOUDINI_USE_QT6__", "0") == "1":
    import PySide6 as _internal_pyside

    # QAction and QActionGroup are commonly used so make it available here.
    from PySide6.QtGui import QAction, QActionGroup

    isQt6 = True
else:
    import PySide2 as _internal_pyside

    # QAction and QActionGroup are commonly used so make it available here.
    from PySide2.QtWidgets import QAction, QActionGroup

    isQt6 = False


# Register submodules.
# Call `pkgutil.walk_packages()` to discover PySide submodules.
for submodule_info in pkgutil.walk_packages(_internal_pyside.__path__):
    try:
        # Import the PySide submodule.
        submodule = importlib.import_module(
            f"{_internal_pyside.__name__}.{submodule_info.name}")

        # Register the submodule as an hutil.PySide submodule.
        sys.modules[f"{__name__}.{submodule_info.name}"] = submodule
    except ImportError:
        # The submodule could not be imported.
        # This can happen in the Qt5/PySide2 where not all Qt libraries
        # (i.e. QtPositioningQuick) are packaged in Houdini.
        pass


# Purposely set __all__ to an empty list to discourage people from
# doing `from hutil.PySide import *`.
__all__ = []
```

Механизм: модуль **не** объявляет `QtWidgets`/`QtCore`/`QtGui`/`QtNetwork`
как явные атрибуты пакета — он регистрирует их динамически в
`sys.modules["hutil.PySide.<submodule>"]` через `pkgutil.walk_packages`.
Практическое следствие: правильный импорт — **прямой submodule-импорт**,
как обычный субпакет:

```python
from hutil.PySide import QtWidgets, QtCore, QtGui, QtNetwork
```

Это работает благодаря тому, что имена уже лежат в `sys.modules` к моменту
импорта (регистрация происходит в момент выполнения `hutil/PySide/__init__.py`,
то есть при первом `import hutil.PySide` или `from hutil.PySide import ...`).
Правило проекта "Qt только через `hutil.PySide`" соответствует именно этому
факту — модуль подставляет PySide6 на Qt6-сборках Houdini (22.0) и PySide2
на Qt5-сборках (20.5) прозрачно для остального кода.

Есть также отдельный модуль `hutil/Qt.py` (сторонняя библиотека
[Qt.py от Marcus Ottosson](https://github.com/mottosso/Qt.py), версия
`__version__ = "1.2.3"`, встроена в обе версии Houdini) — это более старый
универсальный шим с приоритетом `PySide6 → PySide2 → PyQt5 → PySide → PyQt4`
и своими переменными окружения (`HOUDINI_QT_VERBOSE`,
`HOUDINI_QT_PREFERRED_BINDING`). Именно его использует пример
`nodepath.pypanel` (`from hutil.Qt import QtWidgets`). Это не то же самое,
что `hutil.PySide` — оба существуют одновременно, но правило проекта
называет именно `hutil.PySide`, поэтому использовать нужно его, а не
`hutil.Qt`.

### Версии Qt/PySide (проверено запуском интерпретатора Houdini)

- **Houdini 20.5.445**: `PySide2.__version__ == "5.15.15"` (Qt 5.15.15),
  пакет лежит в
  `.../Houdini20.5.445/Frameworks/Python.framework/Versions/3.11/lib/python3.11/site-packages-forced/PySide2`.
  Рядом — `shiboken2`, `shiboken2_generator`.
- **Houdini 22.0.368**: `PySide6` версии `6.8.3`
  (`.../Houdini22.0.368/.../site-packages-forced/PySide6-6.8.3-py3.13.egg-info`),
  пакет в
  `.../Houdini22.0.368/Frameworks/Python.framework/Versions/3.13/lib/python3.13/site-packages-forced/PySide6`.

### Наличие `QtWebEngineWidgets`, `QtWebSockets`, `QtMultimedia`

Проверено реальным импортом в обеих версиях (не просто наличием `.pyi`
стаба — рантайм-модуль импортируется без ошибок):

```
PySide2 (20.5, Qt 5.15.15): QtWebEngineWidgets OK, QtWebSockets OK, QtMultimedia OK, QtNetwork OK
PySide6 (22.0, Qt 6.8.3):   QtWebEngineWidgets OK, QtWebSockets OK, QtMultimedia OK, QtNetwork OK
```

Все четыре модуля доступны в обеих версиях — можно строить сетевой слой
(ACP по WebSocket/stdio) и веб-контент без опасений про их отсутствие.

Дополнительно: 22.0 (PySide6) не содержит модуль `QtWebEngine.pyi` (он был в
Qt5/PySide2 как объединяющий модуль), вместо него — раздельные
`QtWebEngineCore`, `QtWebEngineWidgets`, `QtWebEngineQuick`. В 20.5
(PySide2) есть отдельный `QtWebEngine.pyi` в дополнение к тем же трём.

---

## 4. Версия Python и директории автозагрузки

Проверено прямым запуском бинарника Python из Houdini:

```
Houdini 20.5.445 → Python 3.11.7 (main, May  9 2024, ...) [Clang 15.0.0]
Houdini 22.0.368 → Python 3.13.10 (main, Mar  4 2026, ...) [Clang 15.0.0]
```

Соответствующие директории автозагрузки user-скриптов (подтверждены самим
существованием и содержимым, например `hutil` лежит именно там):
- Houdini 20.5 → `python3.11libs` (например
  `.../Houdini20.5.445/.../Resources/houdini/python3.11libs/hutil/...`)
- Houdini 22.0 → `python3.13libs` (например
  `.../Houdini22.0.368/.../Resources/houdini/python3.13libs/hutil/...`)

Любая директория `python3.11libs`/`python3.13libs`, найденная в любом из
путей `HOUDINI_PATH` (включая пути пакетов), автоматически добавляется в
`sys.path` — так `hutil`, `toolutils`, `hdefereval` и т.д. становятся
импортируемыми без ручного добавления в `PYTHONPATH`.

Момент выполнения (по документации SideFX; в установке не хранится как
пример, т.к. это user-скрипты, создаваемые самим художником —
подтверждено отсутствием файлов `123.py`/`pythonrc.py` по умолчанию в
`$HFS/houdini/scripts/`, там нашлась только `scripts/` и `config/Scripts`
директории, без этих конкретных файлов):
- `$HOUDINI_USER_PREF_DIR/scripts/pythonrc.py` — выполняется один раз при
  запуске Houdini, до первого открытия сцены/UI.
- `$HOUDINI_USER_PREF_DIR/scripts/123.py` — выполняется при каждом создании
  новой сцены и при загрузке `.hip`-файла (аналог `456.cmd`/`123.cmd` для
  hscript, только для Python).
- `uiready.py` — **подтверждено физически**, файл существует:
  `.../Houdini22.0.368/.../Resources/houdini/python3.13libs/uiready.py`.
  По документации SideFX выполняется после того, как UI Houdini полностью
  готов (когда можно безопасно создавать Qt-виджеты, открывать панели и
  т.п.) — более поздняя точка, чем `123.py`, которая может выполняться до
  готовности UI в batch/hbatch режиме.

---

## 5. Тёмная тема / стилизация Qt

Подтверждено исходным кодом `hou.py` (SWIG-обёртка, реальные докстринги):

```python
def colorFromName(self, name: "char const *") -> "HOM_Color":
    r"""
    colorFromName(self, name) -> hou.Color
    ...
      > >>> hou.ui.colorFromName("DisplayOnColor")
    """
    return _hou.ui_colorFromName(self, name)
```

То есть `hou.ui.colorFromName("ИмяЦвета")` — рабочий, задокументированный
способ достать конкретный цвет темы Houdini (имена цветов вроде
`GraphDisplayHighlight`, `GraphRenderHighlight`, `DisplayOnColor` — взяты
из докстрингов соседних функций в том же файле, строки ~80410-80426).

Отдельного публичного метода для получения qss-таблицы стилей Houdini
(что-то вроде `hou.ui.qtStyleSheet()`) в `hou.py` не встретилось при поиске
по этому файлу — не подтверждаю его существование. Стандартная практика в
панелях SideFX — не переопределять глобальный QSS, а точечно красить
через палитру приложения (`QtWidgets.QApplication.palette()`, уже настроенную
Houdini) и через `hou.ui.colorFromName` для акцентных цветов, оставляя
базовые виджеты (кнопки, поля) на нативном стиле Houdini — тогда панель
выглядит нативно "бесплатно", без своего qss.

---

## 6. `$HIP`

Подтверждено докстрингами `hou.py`:

```python
def path(self) -> "std::string":   # hou.hipFile.path()
    r"""
    path() -> str
        Return the absolute file path of the current scene file. Remember
        that a file may not exist at this path if the current scene hasn't
        been saved yet.
    """
```

```python
def expandString(self, str: "char const *", expand_tilde: "bool"=True) -> "std::string":
    r"""
    expandString(str, expand_tilde=True) -> str
      > >>> hou.text.expandString("$HIP/file.geo")
    """
    return _hou.text_expandString(self, str, expand_tilde)
```

Итого два рабочих способа:
- `hou.hipFile.path()` — абсолютный путь к текущему `.hip`-файлу.
- `hou.text.expandString("$HIP/...")` — разворачивает переменную Houdini
  `$HIP` внутри произвольной строки (годится для путей с плейсхолдерами).

`hou.hipFile.isNewFile()` (метод присутствует в `hou.py`, класс `hipFile`,
строка ~65590 в файле 22.0) — способ узнать, что сцена ещё не сохранялась
(несохранённая новая сцена); для неё `path()` вернёт путь по умолчанию
Houdini подставляет вроде `untitled.hip` в текущей рабочей директории —
это поведение не проверялось живым запуском Houdini в рамках этой задачи
(интерпретатор `hou` не удалось запустить standalone вне самого приложения
из-за отсутствия части рантайм-окружения/dylib, см. раздел 8), поэтому
дословное значение для новой сцены не подтверждаю напрямую — только то, что
через `hou.hipFile.isNewFile()` можно на это отдельно проверить перед тем,
как доверять `path()`.

---

## 7. Главный поток и безопасный вызов `hou` из фонового потока

Подтверждено исходным кодом модуля `hdefereval.py`, который реально
существует в обеих версиях:
`.../Houdini22.0.368/.../Resources/houdini/python3.13libs/hdefereval.py`.

Докстринг модуля (дословно):
```python
"""This module provides functions to perform deferred evaluation of Python
code.  You can call these functions from any thread, and they are executed
in the main thread when Houdini's event loop is idle.
"""
```

Сигнатуры (дословно):
```python
def executeDeferred(code, *args, **kwargs):
    """Run the specified Python code in the main thread and do not wait
    for it to finish running.

    code: Either a string containing a Python expression, a callable object,
        or a code object.
    args, kwargs: Only valid for callable objects.
    """

def executeDeferredAfterWaiting(code, num_waits, *args, **kwargs):
    """This function is like executeDeferred, except it waits for the event
    loop callback to be triggered the given number of times before running
    the callback.

    Use this function when starting Houdini with a script that needs to run
    after Houdini's UI has fully initialized.
    """

def executeInMainThreadWithResult(code, *args, **kwargs):
    return _queueDeferred(code, args, kwargs, block=True)
```

Практическое правило (соответствует правилу проекта "`hou` не трогаем из
рабочего потока"):
- Из фонового Python-потока панели: звать `hou`-код только через
  `hdefereval.executeDeferred(callable, *args, **kwargs)` (fire-and-forget,
  выполнится на главном потоке на следующем цикле событий) или
  `hdefereval.executeInMainThreadWithResult(callable, *args, **kwargs)`
  (блокирует вызывающий поток до выполнения на главном и возвращает
  результат).
- `hou.ui.postEventCallback(callback)` — тоже задокументирован в `hou.py`
  (строка ~105389, дословно):
  ```python
  def postEventCallback(self, callback: "InterpreterObject") -> "void":
      r"""
      postEventCallback(callback)

          Register a Python callback to be called next in Houdini's event
          loop. This will be called only once.

          callback
              Any callable Python object that expects no parameters. It could
              be a Python function, a bound method, or any object implementing
              __call__.
      """
  ```
  В отличие от `hdefereval`, у `hou.ui.postEventCallback` колбэк без
  аргументов и без возврата результата вызывающему — то есть это более
  низкоуровневый примитив, а `hdefereval` — удобная обёртка поверх
  аналогичного механизма, ориентированная именно на межпоточные вызовы.
- Проверка "можно ли трогать `hou` прямо сейчас" —
  `hou.isUIAvailable()` (докстринг из `hou.py`, дословно):
  ```python
  def isUIAvailable() -> "bool":
      r"""
      hou.isUIAvailable
      Return whether or not the hou.ui module is available.
      USAGE
        isUIAvailable() -> bool
      The hou.ui module is not available in the command-line interpreter or in
      MPlay, and this function helps you to write scripts that will run in
      Houdini and command-line and/or MPlay.
      """
  ```
  Важно: это про доступность `hou.ui` (batch/hbatch vs full UI), а не про
  "сейчас ли главный поток". Отдельного публичного API "являюсь ли я
  главным потоком" в `hou.py` при поиске не встретилось — стандартная
  практика (в т.ч. подтверждаемая самим существованием `hdefereval`) —
  просто никогда не считать себя на главном потоке внутри
  worker/QThread панели и всегда идти через `hdefereval`, а не пытаться
  динамически определять поток.

---

## 8. Ограничения этого исследования

- `hou` не удалось импортировать standalone-Python'ом Houdini 22.0 вне
  самого приложения — падает на `dlopen` (`Symbol not found: _iconv` в
  `libfbxsdk.dylib` при неполном `DYLD_LIBRARY_PATH`/окружении). Все факты
  про `hou.*` в этом документе взяты из исходного текста `hou.py`
  (SWIG-обёртка с полными докстрингами) и из докстрингов `hipFile`/`ui`/
  `text`/`isUIAvailable`, а не из живого вызова внутри Houdini — то есть
  сигнатуры и докстринги подтверждены дословно, но фактическое поведение
  в момент выполнения (тип возвращаемого значения на новой несохранённой
  сцене и т.п.) не перепроверялось живым запуском в рамках этой задачи.
- В `hou.py` не найден метод вида `hou.ui.qtStyleSheet()` — если он
  существует, он не встретился при grep по этому файлу; не утверждаю ни
  наличие, ни отсутствие сверх этого.

---

## 9. asyncio внутри Houdini — `haio` (найдено при интеграции, ломает наивный QThread)

Houdini подменяет политику asyncio своей: `asyncio.get_event_loop_policy()`
возвращает `haio.HoudiniEventLoopPolicy`, а `asyncio.new_event_loop()` —
`haio.HoudiniEventLoop`. Проверено запуском в обеих версиях
(`python3.11libs/haio.py`, `python3.13libs/haio.py`).

Два следствия, каждое ломает клиент, написанный «как обычно»:

**1. Цикл Houdini работает только на главном потоке.**
```
RuntimeError: Current thread is not the main thread
  haio.py(116): check_thread
  haio.py(2116): run_forever
```
То есть `asyncio.new_event_loop()` + `run_forever()` на рабочем QThread падает.
Лечится тем, что класс цикла берётся напрямую, минуя политику:
```python
loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.SelectorEventLoop()
```

**2. Подпроцесс через `asyncio.create_subprocess_exec` не поднимается.**
Стоковый `_UnixSelectorEventLoop._make_subprocess_transport` идёт за child
watcher через политику, а у Houdini она его не даёт:
```
  asyncio/unix_events.py(202): watcher = events.get_child_watcher()
  asyncio/events.py(842): return get_event_loop_policy().get_child_watcher()
  haio.py(3084): raise NotImplementedError
```
Значит `acp.spawn_agent_process()` внутри Houdini не работает — он построен
именно на `create_subprocess_exec`.

Рабочий обход, проверенный запуском в Houdini 22.0.368 и 20.5.445: поднимать
процесс обычным `subprocess.Popen`, а его каналы заводить в цикл публичным
API, которому watcher не нужен:
```python
proc = subprocess.Popen(argv, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env, cwd=cwd)

reader = asyncio.StreamReader(limit=50 * 1024 * 1024, loop=loop)
await loop.connect_read_pipe(
    lambda: asyncio.StreamReaderProtocol(reader, loop=loop), proc.stdout)
transport, protocol = await loop.connect_write_pipe(
    lambda: asyncio.streams.FlowControlMixin(loop=loop), proc.stdin)
writer = asyncio.StreamWriter(transport, protocol, reader, loop)

conn = acp.connect_to_agent(client, writer, reader)   # байтовая форма
```
`connect_to_agent` принимает пару StreamWriter/StreamReader — это
задокументированная форма вызова (см. [`acp-sdk.md`](acp-sdk.md) §1), так что
обход остаётся на публичном API SDK.

Почему это не поймали юнит-тесты: вне Houdini политика стоковая, и оба вызова
работают. Тест, который ловит регрессию, обязан подставлять политику,
имитирующую `haio` — падающую на `run_forever()` не в главном потоке и
бросающую `NotImplementedError` из `get_child_watcher()`.
