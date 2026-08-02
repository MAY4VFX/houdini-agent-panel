# Houdini 22 UI: как нативно встроить Agent Panel

**TL;DR:** отдельного публичного SideFX design system / Figma / UI style guide для Houdini 22 не найдено.  
Публичный дизайн-контракт H22 — новая тема Houdini + стандартные Qt-виджеты + `hou.qt` + масштабирование через `hou.ui`; его достаточно, чтобы панель выглядела штатной во всех темах.  
Для Agent Panel стоит наследовать host style, брать цвета/иконки/размеры у Houdini и оставить собственный styling только для специфичных сущностей чата.

Дата проверки: 2026-08-02. Целевая локальная сборка: Houdini 22.0.368.

## Короткий ответ

У SideFX есть не документ с визуальными правилами в духе Material Design, а набор
исполняемых primitives:

1. Houdini 22 получил новый UI skin и Theme Editor.
2. Theme Editor выводит всю палитру из трёх смысловых цветов: Base, Accent и
   Highlight; пользователь может менять тему и контраст.
3. `hou.qt` отдаёт штатные Houdini widgets, stylesheet, resource colors и SVG icons.
4. `hou.ui.scaledSize()` / `globalScaleFactor()` синхронизируют размеры с настройкой
   Global UI Size.
5. Python Panel — штатный способ встроить PySide6/PyQt6 widget в pane tab.

Поэтому «органично вписаться» означает не воспроизвести один тёмный скриншот H22,
а оставаться корректным при другой Base/Accent/Highlight-теме, UI scale и платформе.

Поиск также находит страницу с заголовком `Style guide`, но она относится только
к info panels внутри viewport Python state HUD, а не к Qt pane tabs: [HUD info
style guide](https://www.sidefx.com/docs/houdini/hom/hud_info.html).

SideFX staff отдельно поясняет практический предел нативности H22: интерфейс
смешивает traditional Houdini UI, QML и QtWidgets; их постарались свести визуально,
но некоторые различия остаются. Поэтому custom Qt panel может быть нативной, но не
обязана быть pixel-identical QML-панелям ([официальный форум, ответ SideFX
staff](https://www.sidefx.com/forum/topic/104193/)).

## Что SideFX действительно документирует

### 1. H22 — новый theme-driven UI

SideFX прямо пишет, что Houdini 22 вводит новый UI skin, настраиваемый через Theme
Editor; старый UI deprecated и исчезнет в следующих версиях. Theme Editor строит
палитру из Base, Accent и Highlight и эвристически поддерживает контраст и
читаемость. Accent используется для кнопок и цветных частей UI-иконок, Highlight —
для current/selected state. Светлые темы пока названы experimental, но они уже
существуют, поэтому код не должен предполагать тёмный фон.

Источники:

- [What's new: User interface and viewport](https://www.sidefx.com/docs/houdini/news/22/viewport.html)
- [Theme Editor](https://www.sidefx.com/docs/houdini/ref/windows/theme_editor.html)

### 2. Python Panel — штатный контейнер

Python Panel встраивает PySide6/PyQt6 interface в pane tab. Корневой widget
возвращает `onCreateInterface()`. Qt UI и методы `hou.PythonPanel` должны вызываться
только из main Houdini thread.

Источники:

- [Python Panel](https://www.sidefx.com/docs/houdini/ref/panes/pythonpanel.html)
- [Python Panel Editor](https://www.sidefx.com/docs/houdini/ref/windows/pythonpaneleditor.html)
- [`hou.PythonPanel`](https://www.sidefx.com/docs/houdini/hom/hou/PythonPanel.html)
- [HOM Qt cookbook](https://www.sidefx.com/docs/houdini/hom/cb/qt.html)

### 2.1. Точный стек H22 и важное расхождение по импорту

Официальная platform page для Houdini 22 указывает Qt 6.8.3, PySide6 6.8.3 и
Python 3.13.10 (также доступен Python 3.11 build); Qt 5 builds больше нет.

Источник: [Houdini 22 system requirements and supported platforms](https://www.sidefx.com/docs/houdini/news/22/platforms.html).

Официальные H22 примеры показывают прямой `from PySide6 import ...`, а комментарии
в нескольких factory `.pypanel` из локальной установки прямо называют
`hutil.PySide` internal-use only и советуют стороннему коду импортировать PySide
напрямую. Это противоречит нашему локальному правилу «Qt только через
`hutil.PySide`».

Решение проекта остаётся осознанным compatibility layer для одного кода на H20.5
и H22, но его нельзя выдавать за рекомендацию публичной H22 документации SideFX.
Это наш trade-off; при проблемах совместимости shim будет первым подозреваемым.

### 3. Наследование Houdini stylesheet

`hou.qt.styleSheet()` возвращает Houdini stylesheet. Если передать путь к своему
QSS, Houdini разворачивает в нём свои placeholders: resource colors вроде
`@MenuBG@` и scale-aware размеры вроде `@14px@`. Дочерние widgets наследуют style
родителя; документация `hou.qt.mainWindow()` отдельно подтверждает, что parent к
главному окну наследует Houdini stylesheet.

Это даёт два режима:

- обычные widgets внутри Python Panel: ничего глобально не перекрашивать, позволить
  Qt унаследовать host style;
- специфичный chat widget: загрузить маленький локальный QSS через
  `hou.qt.styleSheet(path)`, используя tokens Houdini вместо hex и обычных `px`.

Источники:

- [`hou.qt.styleSheet`](https://www.sidefx.com/docs/houdini/hom/hou/qt/styleSheet.html)
- [`hou.qt.mainWindow`](https://www.sidefx.com/docs/houdini/hom/hou/qt/mainWindow.html)

### 4. Цвета — resource names, не hex

`hou.qt.getColor(name)` возвращает `QColor` для именованного Houdini resource color;
имена определены в текущих `.hcs` color scheme files. Для custom painting это более
точный Houdini API, чем копирование RGB из factory dark theme. Для обычных
`QWidget` ещё проще использовать текущий `QPalette`.

Практическое разделение:

- `QPalette.Window`, `Text`, `Base`, `Button`, `Highlight` — базовый custom paint;
- `hou.qt.getColor("SecondaryText")`, `getColor("IconError")` и другие resource
  tokens — только когда нужна отсутствующая в `QPalette` семантика;
- не кешировать вычисленный цвет как вечную константу: пользователь может сменить
  тему во время сессии.

Источник: [`hou.qt.getColor`](https://www.sidefx.com/docs/houdini/hom/hou/qt/getColor.html).

Последний пункт про обновление кеша — наша инженерная рекомендация, а не явно
сформулированное правило SideFX.

### 5. Иконки — штатный Houdini icon registry

`hou.qt.Icon(icon_name)` создаёт `QIcon` из Houdini icon name. Имена можно смотреть
через file chooser по `hicon://`; старый `hou.qt.createIcon()` deprecated. Это
предпочтительнее буквенных бейджей и Unicode-символов там, где у Houdini есть ясная
семантическая иконка.

Источники:

- [`hou.qt.Icon`](https://www.sidefx.com/docs/houdini/hom/hou/qt/Icon.html)
- [`hou.qt.createIcon` (deprecated)](https://www.sidefx.com/docs/houdini/hom/hou/qt/createIcon.html)

### 6. Размеры — Global UI Size

`hou.ui.scaledSize(n)` масштабирует hard-coded size по Houdini Global UI Size.
`hou.ui.globalScaleFactor()` нужен там, где API принимает коэффициент, например для
`QWebEngineView`. QSS placeholders `@Npx@` дают тот же принцип декларативно.

Источник: [`hou.ui`](https://www.sidefx.com/docs/houdini/hom/hou/ui.html).

### 6.1. Шрифты — наследовать, а не фиксировать

Публичного typography scale или `hou.qt.getFont(token)` не найдено. Безопасный
контракт — наследовать application/widget font, меняя только weight или размер
относительно текущего font. H22 включает Routed Gothic, но platform page не обещает
его как стабильный public UI-font contract. Factory QSS использует `@FontFixed@`,
однако публичная документация явно гарантирует лишь сам механизм placeholders, а не
полный стабильный список font tokens.

Практический вывод: обычный текст наследует host font; для кода подходит
`QFontDatabase.systemFont(QFontDatabase.FixedFont)`, уже используемый проектом.

### 7. Готовые Houdini-look widgets

Публичный `hou.qt` включает `Menu`, `MenuButton`, `ComboBox`, `SearchLineEdit`,
`FieldLabel`, `Separator`, `ToolTip`, `HelpButton`, `FileChooserButton`,
`NodeChooserButton`, `GridLayout` и другие классы, прямо описанные как widgets с
Houdini look and feel или стабильной cross-platform layout geometry.

Для Agent Panel особенно уместны:

- `hou.qt.Menu` / `MenuButton` для выбора агента, режима и сессии;
- `hou.qt.SearchLineEdit` на экране агентов;
- `hou.qt.Separator` вместо нарисованных линий;
- `hou.qt.ToolTip` для Houdini-native подсказок;
- `hou.qt.Icon` для toolbar actions.

Источник: [`hou.qt` package](https://www.sidefx.com/docs/houdini/hom/hou/qt/index.html).

## Что показывает установленный Houdini 22.0.368

Это наблюдения по factory files, а не обещанный публичный API.

Проверены:

- `$HFS/houdini/config/Styles/base.qss`;
- `$HFS/houdini/config/UIDark.hcs` и `UILight.hcs`;
- `$HFS/houdini/config/Icons/SVGIcons.index`;
- 35 factory `.pypanel` в `$HFS/houdini/python_panels`.

Локальная сборка подтверждает Python 3.13.10, Qt 6.8.3 и PySide6 6.8.3.

Наблюдения:

- factory QSS сам использует токены (`@BackColor@`, `@TextColor@`,
  `@ButtonGradHi@`, `@SelectedTextBG@`) и scale placeholders (`@1px@`, `@17px@`);
- базовая геометрия плотная: line edit и tool button около 17 px при normal scale,
  tabs около 20 px, checkbox/radio indicator около 14 px;
- радиусы сдержанные: tool button 4 px, group box 5 px, tabs квадратные;
- native controls важнее custom card styling: списки, поля, меню, tabs и scrollbars
  уже согласованы stylesheet-ом;
- штатные панели в основном являются тонкой `.pypanel`-обёрткой над Qt/QML module,
  а не копируют общий stylesheet в каждом интерфейсе;
- сам factory code иногда вызывает `setStyleSheet(hou.qt.styleSheet())` для detached
  menus и сложных panel roots.

Из этого не следует, что надо копировать `base.qss` или зависеть от его точных
селекторов: файл внутренний и может измениться. Он полезен как визуальный reference,
а публичные методы `hou.qt` — как runtime contract.

### Какие штатные панели смотреть как референс

- `hrecipes/manager.py` (Recipe Manager) — compact header, notices, search/list
  states;
- `lightmixer/lmui.py` и `lmmixer.py` (Light Mixer) — toolbar, tree, custom cells,
  scoped QSS;
- `scenegraphdetails/` — split panes, toolbar, tables, вторичная иерархия;
- `pdgdatalayer/datalayerpanel.py` — явный `hou.qt.styleSheet()` и scaled geometry;
- `paintinstances/panel.py`, `charpicker/controlbutton.py` — `hou.qt.Icon`,
  `getColor`, state styling.

Это локальные implementation references H22.0.368, не публичные API. Private
dynamic properties из factory QSS (`plain`, `transparent`, `field_label`) копировать
в продукт не стоит.

## Рекомендованный визуальный код для Agent Panel

### Иерархия решений

1. Сначала standard Qt widget без локального stylesheet.
2. Если есть подходящий публичный `hou.qt` widget — использовать его.
3. Для custom paint — текущий `QPalette`; для Houdini-specific semantics —
   `hou.qt.getColor()`.
4. Для custom QSS — отдельный короткий файл с `@ColorToken@` и `@Npx@`, обработанный
   `hou.qt.styleSheet(path)`.
5. Для размеров layout/custom paint — один `scaled(n)` adapter вокруг
   `hou.ui.scaledSize(n)` с fallback `n` для тестов вне Houdini.
6. Иконки — `hou.qt.Icon`; свой SVG только если Houdini registry не имеет нужной
   продуктовой семантики.

Проектное правило импорта остаётся сильнее примеров SideFX: Qt берём только через
`hutil.PySide`; `hou` допустим лишь в UI/main thread и через маленький host adapter,
чтобы widgets оставались тестируемыми без Houdini.

### Минимальный adapter

Это направление кода, не готовый patch:

```python
# ui/host_style.py — вызывается только из Houdini UI/main thread
try:
    import hou
except ImportError:
    hou = None

from .qt import QtGui, QtWidgets


def scaled(value: int) -> int:
    return hou.ui.scaledSize(value) if hou is not None else value


def icon(name: str) -> QtGui.QIcon:
    if hou is not None and hou.qt.canCreateIcon(name):
        return hou.qt.Icon(name)
    return QtGui.QIcon()


def color(resource: str, fallback_role=QtGui.QPalette.Text) -> QtGui.QColor:
    if hou is not None:
        try:
            return hou.qt.getColor(resource)
        except hou.OperationFailed:
            pass
    return QtWidgets.QApplication.palette().color(fallback_role)
```

В реальной реализации импорт `hou` стоит изолировать ещё жёстче, чтобы модуль
невозможно было случайно вызвать из ACP worker thread.

### Что делать с чатом, который не является штатным Houdini control

У transcript, tool call, permission request и composer нет прямых аналогов в HOM.
Здесь допустим собственный язык, но он должен быть «надстройкой над Houdini»:

- сообщения — без bubble-card рамки по умолчанию; разделение отступом и типографикой;
- tool call — компактная строка высотой native control, раскрытие по клику;
- permission — штатные `QPushButton`, primary action через Accent/Highlight, не через
  фирменный hex;
- composer — `QPlainTextEdit`/`QTextEdit` с host field background и native border;
- toolbar actions — `QToolButton` с Houdini icons, 16–18 logical px;
- status различать формой + текстом, не только цветом;
- spacing держать плотным: базовая сетка 4 px, наружный gutter 8 px, но оба значения
  обязательно scale-aware.

Последний набор — вывод из factory QSS и устройства панели, не официальный SideFX
гайд.

## Аудит текущего кода

Текущий [`ui/theme.py`](../python/houdini_agent_panel/ui/theme.py) уже делает главное
правильно: использует `QApplication.palette()` и не хардкодит dark-theme hex.

Что стоит улучшить отдельной задачей:

1. Масштабировать `SPACING`, `MARGIN`, `RADIUS`, `ICON_SIZE` и остальные literal
   sizes через host adapter. Сейчас UI scale Houdini не учитывается.
2. Заменить `color: gray` в `ui/agents.py` на palette/resource semantic.
3. Не задавать `font-size: 14px` в `ui/auth_view.py`; использовать host font с
   bold/relative size.
4. Проверить Houdini icon registry для add/settings/send/stop/attachment и tool kinds;
   использовать `hou.qt.Icon` там, где семантика совпадает. Текстовые бейджи оставить
   осознанным fallback.
5. Не применять full Houdini stylesheet ко всему panel root без нужды: panel уже
   встроен в styled tree. Явно стилизовать только detached popups/menus или локальные
   custom selectors.
6. Проверять H22 как минимум на Houdini Dark, одной сильно изменённой custom theme,
   experimental light theme и UI scale 1.0/1.5/2.0, а также в узком и широком dock.

## Вывод

У SideFX нет найденного отдельного учебника «как рисовать красивый Houdini UI», но
у Houdini 22 есть более полезная вещь для embedded plugin: живая theme system и
публичный Qt integration layer. Для этой панели правильная стратегия — не делать
Houdini-подобную тему самим, а дать Houdini рисовать всё стандартное и построить
только chat-specific components поверх его цветов, metrics и icons.
