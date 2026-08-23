from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
from pathlib import Path
import shutil
from typing import Any, Callable

from .chart_notes import write_note_counts_tsv


RESOURCE_DIR_MAP = {
    "avatar": "avatars",
    "chart": "charts",
    "illustration": "illustrations",
    "illustrationBlur": "illustrations-blur",
    "illustrationLowRes": "illustrations-lowres",
    "music": "music",
    "metadata": "metadata",
}

_CONTENT_TYPE_OVERRIDES = {
    ".ogg": "audio/ogg",
}


def _safe_version(value: str) -> str:
    cleaned = "".join(char for char in value if char.isalnum() or char in ".-_")
    if not cleaned:
        raise ValueError("资源版本号为空或包含非法字符")
    return cleaned


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _content_type(path: Path) -> str:
    if path.suffix.lower() in _CONTENT_TYPE_OVERRIDES:
        return _CONTENT_TYPE_OVERRIDES[path.suffix.lower()]
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _load_tsv(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as source:
        return [row for row in csv.reader(source, delimiter="\t") if row]


def build_catalog(metadata_dir: Path) -> dict[str, Any]:
    difficulty_rows = {row[0]: row[1:] for row in _load_tsv(metadata_dir / "difficulty.tsv")}
    songs: list[dict[str, Any]] = []
    for row in _load_tsv(metadata_dir / "info.tsv"):
        if len(row) < 4:
            continue
        songs.append(
            {
                "id": row[0],
                "title": row[1],
                "composer": row[2],
                "illustrator": row[3],
                "charters": row[4:],
                "difficulties": [float(value) for value in difficulty_rows.get(row[0], [])],
            }
        )
    return {"schemaVersion": 1, "songCount": len(songs), "songs": songs}


def organize_release(
    extracted_output: Path,
    release_root: Path,
    game_version: str,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    version = _safe_version(game_version)
    phigros_root = release_root / "phigros"
    version_dir = phigros_root / "releases" / version
    if version_dir.exists():
        shutil.rmtree(version_dir)
    version_dir.mkdir(parents=True, exist_ok=True)

    for source_name, target_name in RESOURCE_DIR_MAP.items():
        source = extracted_output / source_name
        if source.exists():
            shutil.copytree(source, version_dir / target_name, dirs_exist_ok=True)

    metadata_dir = version_dir / "metadata"
    catalog_path = version_dir / "catalog.json"
    catalog_path.write_text(
        json.dumps(build_catalog(metadata_dir), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    charts_dir = version_dir / "charts"
    note_counts = write_note_counts_tsv(
        charts_dir,
        metadata_dir / "note_counts.tsv",
        metadata_dir,
    )

    files = sorted(
        path for path in version_dir.rglob("*") if path.is_file() and path.name != "manifest.json"
    )
    assets: list[dict[str, Any]] = []
    total = len(files)
    for index, path in enumerate(files, start=1):
        relative = path.relative_to(version_dir).as_posix()
        assets.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "contentType": _content_type(path),
            }
        )
        if progress:
            progress(index, total, relative)

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = {
        "schemaVersion": 1,
        "gameVersion": version,
        "generatedAt": generated_at,
        "assetCount": len(assets),
        "totalBytes": sum(item["size"] for item in assets),
        "assets": assets,
    }
    manifest_path = version_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    current = {
        "schemaVersion": 1,
        "gameVersion": version,
        "resourceVersion": f"{version}-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}",
        "publishedAt": generated_at,
        "manifest": f"phigros/releases/{version}/manifest.json",
        "catalog": f"phigros/releases/{version}/catalog.json",
        "noteCounts": f"phigros/releases/{version}/metadata/note_counts.tsv",
    }
    current_path = phigros_root / "current.json"
    current_path.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "version": version,
        "version_dir": str(version_dir),
        "release_root": str(release_root),
        "current_path": str(current_path),
        "asset_count": len(assets),
        "total_bytes": manifest["totalBytes"],
        "note_counts": note_counts,
        "current": current,
    }

