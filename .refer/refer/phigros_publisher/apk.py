from __future__ import annotations

import re
from pathlib import Path


_APK_NAME = re.compile(r"^phigros[_-](.+)$", re.IGNORECASE)


def parse_apk_version(apk_path: Path, override: str | None = None) -> str:
    if override and override.strip():
        return _safe_version(override.strip())
    match = _APK_NAME.match(apk_path.stem)
    if match:
        return _safe_version(match.group(1))
    raise ValueError(
        f"无法从 APK 文件名推断版本：{apk_path.name}。"
        "请使用 Phigros_<版本>.apk 命名，或在请求中提供 game_version。"
    )


def validate_apk_path(value: str | Path) -> Path:
    apk_path = Path(value).expanduser().resolve()
    if not apk_path.is_file():
        raise FileNotFoundError(f"APK 不存在：{apk_path}")
    if apk_path.suffix.lower() != ".apk":
        raise ValueError(f"不是 APK 文件：{apk_path.name}")
    return apk_path


def _safe_version(value: str) -> str:
    cleaned = "".join(char for char in value if char.isalnum() or char in ".-_")
    if not cleaned:
        raise ValueError("资源版本号为空或包含非法字符")
    return cleaned
