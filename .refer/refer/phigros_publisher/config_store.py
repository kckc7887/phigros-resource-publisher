from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import writable_root

DEFAULT_ENDPOINT = "https://cn-nb1.rains3.com"
DEFAULT_BUCKET = "rranker-phigros-data"
DEFAULT_PUBLIC_BASE = "https://rranker-phigros-data.cn-nb1.rains3.com"
DEFAULT_UPLOAD_SCOPE = "all"

CONFIG_FILE_NAME = "config.json"


def default_config() -> dict[str, Any]:
    return {
        "endpoint": DEFAULT_ENDPOINT,
        "bucket": DEFAULT_BUCKET,
        "public_base": DEFAULT_PUBLIC_BASE,
        "access_key": "",
        "secret_key": "",
        "upload_scope": DEFAULT_UPLOAD_SCOPE,
        "remember_keys": True,
        "action": "parse",
        "mode": "local",
        "apk_path": "",
        "music": False,
    }


def config_path(root: Path | None = None) -> Path:
    return (root or writable_root()) / CONFIG_FILE_NAME


def load_config(root: Path | None = None) -> dict[str, Any]:
    path = config_path(root)
    merged = default_config()
    if not path.is_file():
        return merged
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return merged
    if not isinstance(raw, dict):
        return merged
    for key in merged:
        if key in raw:
            merged[key] = raw[key]
    merged["remember_keys"] = bool(merged.get("remember_keys", True))
    return merged


def save_config(data: dict[str, Any], root: Path | None = None) -> Path:
    path = config_path(root)
    payload = default_config()
    for key in payload:
        if key in data:
            payload[key] = data[key]
    remember = bool(payload.get("remember_keys", True))
    payload["remember_keys"] = remember
    if not remember:
        payload["access_key"] = ""
        payload["secret_key"] = ""
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
