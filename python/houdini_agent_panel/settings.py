"""Настройки панели — один маленький JSON.

Читается целиком, пишется атомарно. Частичных мержей нет намеренно: файл на
десяток ключей, а `os.replace` поверх временного файла гарантирует, что упавшая
посреди записи Houdini не оставит человека с обрезанным JSON и без панели.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

SETTINGS_VERSION = 1


@dataclass
class CustomAgent:
    """«Свой агент» — произвольная команда, говорящая по ACP.

    Ни версии, ни скачивания: человек уже поставил это сам, наше дело —
    запустить и не мешать.
    """

    id: str
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class InstalledAgent:
    agent_id: str
    version: str
    kind: str  # "npx" | "binary" | "custom"
    installed_at: str = ""

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Settings:
    version: int = SETTINGS_VERSION
    default_agent: str | None = None
    autostart_agent: bool = True
    check_updates: bool = True
    show_announcements: bool = True
    telemetry: bool = False
    telemetry_consent_asked: bool = False
    whisper_endpoint: str = ""
    buddy: str = "crag"
    custom_agents: list[CustomAgent] = field(default_factory=list)
    installed_agents: dict[str, InstalledAgent] = field(default_factory=dict)
    seen_announcements: list[str] = field(default_factory=list)

    # --- сериализация

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["installed_agents"] = {k: asdict(v) for k, v in self.installed_agents.items()}
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "Settings":
        """Собрать настройки из чего угодно, не падая.

        Неизвестные ключи игнорируются, отсутствующие берутся из дефолтов, а
        ключ неправильного типа откатывается на дефолт. Файл настроек правят
        руками, и опечатка в нём не должна стоить человеку рабочего дня.
        """
        settings = cls()
        if not isinstance(payload, dict):
            return settings

        known = {f.name: f for f in fields(cls)}
        for name, spec in known.items():
            if name not in payload:
                continue
            value = payload[name]
            if name == "custom_agents":
                settings.custom_agents = [
                    CustomAgent(
                        id=str(item.get("id", "")),
                        name=str(item.get("name", "")),
                        command=str(item.get("command", "")),
                        args=[str(a) for a in item.get("args", []) or []],
                        env={str(k): str(v) for k, v in (item.get("env") or {}).items()},
                    )
                    for item in value or []
                    if isinstance(item, dict) and item.get("id") and item.get("command")
                ]
            elif name == "installed_agents":
                installed: dict[str, InstalledAgent] = {}
                for key, item in (value or {}).items():
                    if not isinstance(item, dict):
                        continue
                    installed[str(key)] = InstalledAgent(
                        agent_id=str(item.get("agent_id", key)),
                        version=str(item.get("version", "")),
                        kind=str(item.get("kind", "binary")),
                        installed_at=str(item.get("installed_at", "")),
                    )
                settings.installed_agents = installed
            elif name == "default_agent":
                settings.default_agent = str(value) if value else None
            elif spec.type == "bool" or isinstance(getattr(settings, name), bool):
                settings.__dict__[name] = bool(value)
            elif isinstance(getattr(settings, name), str):
                settings.__dict__[name] = str(value)
            elif isinstance(getattr(settings, name), int):
                try:
                    settings.__dict__[name] = int(value)
                except (TypeError, ValueError):
                    pass
            elif isinstance(getattr(settings, name), list):
                settings.__dict__[name] = [str(v) for v in value or []]
        return settings


def load(path: Path | None = None) -> Settings:
    """Прочитать настройки.

    Битый файл — не ошибка: он отъезжает в ``settings.json.broken``, а человек
    получает панель с дефолтами вместо стектрейса при открытии.
    """
    target = path or paths.settings_path()
    if not target.exists():
        return Settings()
    try:
        payload = json.loads(target.read_text("utf-8"))
    except (OSError, ValueError):
        try:
            target.replace(target.with_suffix(target.suffix + ".broken"))
        except OSError:
            pass
        return Settings()
    return Settings.from_dict(payload)


def save(settings: Settings, path: Path | None = None) -> None:
    target = path or paths.settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2, ensure_ascii=False) + "\n", "utf-8")
    os.replace(tmp, target)


def diagnostics(settings: Settings) -> str:
    """Текст для кнопки «Скопировать диагностику».

    Всё, что нужно для баг-репорта, и ничего, что человек не захотел бы
    отправлять: ни путей к сценам, ни имён проектов, ни содержимого настроек.
    """
    import platform
    import sys

    lines = [
        f"houdini-agent-panel {_panel_version()}",
        f"python {sys.version.split()[0]} ({paths.python_tag()})",
        f"os {platform.platform()}",
    ]

    try:
        from .ui.qt import QT_SOURCE, QT_VERSION

        lines.append(f"qt {QT_VERSION} via {QT_SOURCE}")
    except Exception as exc:  # noqa: BLE001 - диагностика не имеет права падать
        lines.append(f"qt недоступен: {exc!r}")

    try:
        from . import scene

        lines.append(f"houdini {scene.houdini_version()}")
        lines.append(f"fx port {scene.fx_port()}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"houdini недоступна: {exc!r}")

    try:
        import fxhoudinimcp

        lines.append(f"fxhoudinimcp {getattr(fxhoudinimcp, '__version__', 'unknown')}")
    except Exception:  # noqa: BLE001
        lines.append("fxhoudinimcp не импортируется")

    lines.append(f"агент по умолчанию: {settings.default_agent or '—'}")
    for agent_id, installed in sorted(settings.installed_agents.items()):
        lines.append(f"агент {agent_id} {installed.version} ({installed.kind})")
    lines.append(f"обновления: {settings.check_updates}, оповещения: {settings.show_announcements}")
    lines.append(f"телеметрия: {settings.telemetry}")
    return "\n".join(lines)


def _panel_version() -> str:
    try:
        from importlib.metadata import version

        return version("houdini-agent-panel")
    except Exception:  # noqa: BLE001 - из --target-дерева метаданных может не быть
        from . import __version__

        return __version__
