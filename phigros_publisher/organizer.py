from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
from pathlib import Path
import shutil
import subprocess
import re
from uuid import uuid4
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


def validate_music(path: Path) -> bool:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
        "-of", "json", str(path),
    ], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode or result.stderr.strip():
        return False
    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return (stream["codec_name"] == "vorbis" and int(stream["channels"]) > 0
                and int(stream["sample_rate"]) > 0 and float(data["format"]["duration"]) > 0)
    except (KeyError, IndexError, ValueError, TypeError):
        return False


def _check_music(root: Path, music_id: str, missing: list[str]) -> None:
    music = root / "music" / f"{music_id}.ogg"
    if not music.is_file() or music.stat().st_size < 27:
        missing.append(f"music/{music_id}.ogg")
        return
    with music.open("rb") as source:
        header = source.read(64)
    if not header.startswith(b"OggS") or b"\x01vorbis" not in header or not validate_music(music):
        missing.append(f"music/{music_id}.ogg (invalid OGG/Vorbis)")


def validate_catalog_assets(root: Path, catalog: dict[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    songs = catalog["songs"]
    if not songs or catalog["songCount"] != len(songs):
        missing.append("catalog songs")
    for song in songs:
        song_id = song["id"]
        _check_music(root, song_id, missing)
        for directory in ("illustrations", "illustrations-blur", "illustrations-lowres"):
            image = root / directory / f"{song_id}.png"
            if not image.is_file() or image.stat().st_size == 0:
                missing.append(f"{directory}/{song_id}.png")
        chart_root = root / "charts"
        chart_dirs = [path for path in chart_root.iterdir() if path.is_dir()
                      and re.fullmatch(re.escape(song_id) + r"(?:\.\d+)?", path.name)] if chart_root.is_dir() else []
        # The published music is extracted from .0/music.wav. Other numbered charts
        # (e.g. Random's .1-.6) are variants, not duplicate default resources.
        primary = next((path for name in (f"{song_id}.0", song_id)
                        for path in chart_dirs if path.name == name),
                       chart_dirs[0] if len(chart_dirs) == 1 else None)
        for variant in chart_dirs:
            if variant.name not in (song_id, f"{song_id}.0"):
                _check_music(root, variant.name, missing)
        for index, constant in enumerate(song["difficulties"]):
            if constant <= 0:
                continue
            level = ("EZ", "HD", "IN", "AT")[index]
            files = [primary / f"{level}.json"] if primary and (primary / f"{level}.json").is_file() else []
            if len(files) != 1 or files[0].stat().st_size == 0:
                missing.append(f"charts/{song_id}/{level}.json")
    report = {"songCount": len(songs), "musicCount": len(list((root / "music").glob("*.ogg"))),
              "missingResources": missing}
    if missing:
        raise ValueError("发布资源不完整：" + json.dumps(report, ensure_ascii=False))
    return report


def validate_release(release: dict[str, Any]) -> dict[str, Any]:
    root = Path(release["version_dir"]).resolve()
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    report = validate_catalog_assets(root, catalog)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    current = json.loads(Path(release["current_path"]).read_text(encoding="utf-8"))
    if (current.get("manifestSha256") != _sha256(manifest_path)
            or manifest["gameVersion"] != current["gameVersion"]
            or manifest["generatedAt"] != current["publishedAt"]):
        raise ValueError("发布清单与 current.json 不一致")
    listed = set()
    for asset in manifest["assets"]:
        target = (root / asset["path"]).resolve()
        if not target.is_relative_to(root) or asset["path"] in listed:
            raise ValueError("发布清单路径重复或越界")
        listed.add(asset["path"])
        if not target.is_file() or target.stat().st_size != asset["size"] or _sha256(target) != asset["sha256"]:
            raise ValueError(f"发布资源校验失败：{asset['path']}")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name != "manifest.json"}
    if listed != actual or manifest["assetCount"] != len(listed):
        raise ValueError("发布清单文件集合不完整")
    return report


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

    validation = validate_catalog_assets(version_dir, build_catalog(metadata_dir))

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
        "resourceVersion": f"{version}-{uuid4().hex}",
        "manifestSha256": _sha256(manifest_path),
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
        "validation": validation,
        "current": current,
    }

