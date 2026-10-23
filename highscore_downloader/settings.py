"""项目内的可迁移本地配置；程序位置仅持久化为相对路径。"""

from dataclasses import dataclass
import json
import os
from pathlib import Path, PureWindowsPath
import tempfile


CONFIG_NAME = "config.local.json"


@dataclass(frozen=True)
class LocalSettings:
    executable_relative: str | None = None
    auto_discover: bool = True
    message: str = ""


def _relative_value(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("程序位置必须为非空相对路径")
    if Path(value).anchor or PureWindowsPath(value).anchor:
        raise ValueError("程序位置必须相对于项目目录，不能保存绝对路径")
    normalized = value.replace("\\", "/")
    if Path(normalized).name.casefold() != "highscoreplus.exe":
        raise ValueError("程序位置必须指向 HighScorePlus.exe")
    return normalized


def resolve_executable(root: Path, settings: LocalSettings) -> Path | None:
    """从当前项目根目录解释位置，不依赖进程的工作目录。"""
    relative = _relative_value(settings.executable_relative)
    return (Path(root).resolve() / relative).resolve() if relative else None


def load_settings(root: Path) -> LocalSettings:
    """读取失败时保留原文件，返回自动发现设置和中文原因。"""
    config = Path(root).resolve() / CONFIG_NAME
    try:
        if config.is_symlink():
            raise ValueError("配置文件不能为符号链接")
        if not config.exists():
            return LocalSettings()
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("配置内容必须为对象")
        relative = _relative_value(data.get("highscore_executable"))
        auto_discover = data.get("discover_highscore", True)
        if not isinstance(auto_discover, bool):
            raise ValueError("自动发现设置必须为布尔值")
        return LocalSettings(relative, auto_discover or relative is None)
    except (OSError, UnicodeError, ValueError) as error:
        reason = error.strerror if isinstance(error, OSError) else str(error)
        return LocalSettings(message=f"配置读取失败：{reason}；将自动发现程序，也可重新选择。")


def save_settings(root: Path, executable: Path | None) -> LocalSettings:
    """原子保存配置；跨盘位置只保留安装信息自动发现方式。"""
    project_root = Path(root).resolve()
    relative = None
    message = ""
    if executable is not None:
        executable = Path(executable)
        if not executable.is_absolute():
            executable = project_root / executable
        executable = executable.resolve()
        if executable.name.casefold() != "highscoreplus.exe":
            raise ValueError("请选择 HighScorePlus.exe")
        try:
            relative = _relative_value(os.path.relpath(executable, project_root))
        except ValueError:
            message = (
                "所选程序位于其他盘符，本次运行仍可使用；配置只保存自动发现信息。"
                "下次启动会自动发现本机安装位置，未发现时请重新选择。"
            )
    settings = LocalSettings(relative, True, message)
    data = {
        "version": 1,
        "highscore_executable": relative,
        "discover_highscore": True,
        "highscore_discovery": {"program": "HighScorePlus.exe", "method": "windows_installation"},
    }
    config = project_root / CONFIG_NAME
    temporary = None
    try:
        if config.is_symlink():
            raise ValueError("配置文件不能为符号链接")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=project_root,
            prefix=".config-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(config)
    except (OSError, ValueError) as error:
        reason = error.strerror if isinstance(error, OSError) else str(error)
        raise ValueError(f"配置保存失败：{reason}") from error
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return settings
