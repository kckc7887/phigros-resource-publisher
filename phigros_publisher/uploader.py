from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from typing import Any, Callable


UPLOAD_SCOPES = {
    "all": "全部资源（上传后清空 releases，仅保留本次）",
    "current": "仅 current.json",
    "catalog": "仅 catalog.json",
    "manifest": "仅 manifest.json",
    "note_counts": "仅物量表 note_counts.tsv",
    "metadata": "仅 metadata/ 目录",
    "charts": "仅 charts/ 目录",
    "avatars": "仅 avatars/ 目录",
    "illustrations": "仅曲绘（原图/模糊/低清）",
    "music": "仅 music/ 目录",
}

RELEASES_PREFIX = "phigros/releases/"

_CONTENT_TYPE_OVERRIDES = {
    ".ogg": "audio/ogg",
}


def _load_boto3():
    try:
        import boto3
        from botocore.config import Config
    except ImportError as error:
        raise RuntimeError("上传需要 boto3，请先执行 pip install -r requirements.txt") from error
    return boto3, Config


def _make_s3_client(boto3: Any, Config: Any, config: dict[str, Any]) -> Any:
    """Build S3 client tuned for Rainyun / other S3-compatible endpoints."""
    config_kwargs: dict[str, Any] = {
        "signature_version": "s3v4",
        "s3": {"addressing_style": "path"},
    }
    # boto3>=1.36 默认改用 CRC；兼容端常仍强制 DeleteObjects 要 Content-MD5。
    try:
        client_config = Config(
            **config_kwargs,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
    except TypeError:
        client_config = Config(**config_kwargs)
    client = boto3.client(
        "s3",
        endpoint_url=config["endpoint"].rstrip("/"),
        aws_access_key_id=config["access_key"],
        aws_secret_access_key=config["secret_key"],
        config=client_config,
    )
    _register_delete_objects_content_md5(client)
    return client


def _register_delete_objects_content_md5(client: Any) -> None:
    """Inject Content-MD5 for DeleteObjects (required by Rainyun and similar)."""

    def _inject_content_md5(params: dict[str, Any], **_kwargs: Any) -> None:
        body = params.get("body")
        if body is None:
            return
        if hasattr(body, "seek") and hasattr(body, "read"):
            position = body.tell()
            payload = body.read()
            body.seek(position)
        elif isinstance(body, bytes):
            payload = body
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            return
        digest = base64.b64encode(hashlib.md5(payload).digest()).decode("ascii")
        params.setdefault("headers", {})["Content-MD5"] = digest

    client.meta.events.register("before-call.s3.DeleteObjects", _inject_content_md5)


def normalize_upload_scope(value: str | None) -> str:
    scope = (value or "all").strip()
    if scope not in UPLOAD_SCOPES:
        raise ValueError(f"上传范围无效：{scope}，可选：{', '.join(UPLOAD_SCOPES)}")
    return scope


def _match_scope(relative: str, scope: str) -> bool:
    if scope == "all":
        return True
    if scope == "catalog":
        return relative == "catalog.json"
    if scope == "manifest":
        return relative == "manifest.json"
    if scope == "note_counts":
        return relative == "metadata/note_counts.tsv"
    if scope == "metadata":
        return relative == "metadata" or relative.startswith("metadata/")
    if scope == "charts":
        return relative == "charts" or relative.startswith("charts/")
    if scope == "avatars":
        return relative == "avatars" or relative.startswith("avatars/")
    if scope == "illustrations":
        return any(
            relative == prefix or relative.startswith(f"{prefix}/")
            for prefix in ("illustrations", "illustrations-blur", "illustrations-lowres")
        )
    if scope == "music":
        return relative == "music" or relative.startswith("music/")
    return False


def iter_upload_files(
    version_dir: Path,
    scope: str,
) -> list[tuple[Path, str]]:
    """Return (local_path, object_key_suffix_relative_to_version_dir) pairs."""
    scope = normalize_upload_scope(scope)
    if scope == "current":
        return []
    files = sorted(path for path in version_dir.rglob("*") if path.is_file())
    selected: list[tuple[Path, str]] = []
    for path in files:
        relative = path.relative_to(version_dir).as_posix()
        if _match_scope(relative, scope):
            selected.append((path, relative))
    return selected


def upload_release(
    release_result: dict[str, Any],
    config: dict[str, Any],
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    required = ("endpoint", "bucket", "access_key", "secret_key")
    missing = [key for key in required if not str(config.get(key, "")).strip()]
    if missing:
        raise ValueError(f"上传配置缺少：{', '.join(missing)}")

    scope = normalize_upload_scope(config.get("upload_scope"))
    boto3, Config = _load_boto3()
    client = _make_s3_client(boto3, Config, config)
    bucket = config["bucket"]
    version = release_result["version"]
    version_dir = Path(release_result["version_dir"])
    selected = iter_upload_files(version_dir, scope)
    include_current = scope in {"all", "current"}
    total = len(selected) + (1 if include_current else 0)
    if total == 0:
        raise ValueError(f"上传范围 {scope} 没有匹配到任何本地文件")

    uploaded_keys: list[str] = []
    for index, (path, relative) in enumerate(selected, start=1):
        key = f"phigros/releases/{version}/{relative}"
        content_type = (
            _CONTENT_TYPE_OVERRIDES.get(path.suffix.lower())
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )
        client.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "ContentType": content_type,
                "CacheControl": "public, max-age=31536000, immutable",
            },
        )
        uploaded_keys.append(key)
        if progress:
            progress(index, total, key)

    if include_current:
        current_path = Path(release_result["current_path"])
        if not current_path.is_file():
            raise FileNotFoundError(f"缺少 current.json：{current_path}")
        client.upload_file(
            str(current_path),
            bucket,
            "phigros/current.json",
            ExtraArgs={"ContentType": "application/json", "CacheControl": "no-cache, max-age=0"},
        )
        uploaded_keys.append("phigros/current.json")
        if progress:
            progress(total, total, "phigros/current.json")

    deleted = 0
    # 仅全量上传时清空 releases：删除不在本轮上传集合中的对象（含同版本孤儿与其他版本）。
    if scope == "all" and config.get("delete_previous", True):
        keep = {key for key in uploaded_keys if key.startswith(RELEASES_PREFIX)}
        deleted = delete_stale_release_objects(client, bucket, keep)

    public_base = str(config.get("public_base", "")).rstrip("/")
    return {
        "scope": scope,
        "uploaded": len(uploaded_keys),
        "keys": uploaded_keys,
        "deleted_previous": deleted,
        "current_url": f"{public_base}/phigros/current.json" if public_base and include_current else None,
    }


def delete_stale_release_objects(
    client: Any,
    bucket: str,
    keep_keys: set[str],
) -> int:
    """Delete every object under phigros/releases/ that is not in keep_keys."""
    paginator = client.get_paginator("list_objects_v2")
    pending: list[dict[str, str]] = []
    deleted = 0

    def flush() -> None:
        nonlocal pending, deleted
        if not pending:
            return
        try:
            client.delete_objects(Bucket=bucket, Delete={"Objects": pending, "Quiet": True})
            deleted += len(pending)
        except Exception as error:
            # 兼容端若仍拒批量删除，退回逐个删除，保证清理能完成。
            message = str(error)
            if "MissingContentMD5" not in message and "Content-Md5" not in message and "Content-MD5" not in message:
                raise
            for item in pending:
                client.delete_object(Bucket=bucket, Key=item["Key"])
                deleted += 1
        pending = []

    for page in paginator.paginate(Bucket=bucket, Prefix=RELEASES_PREFIX):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key in keep_keys:
                continue
            pending.append({"Key": key})
            if len(pending) >= 1000:
                flush()
    flush()
    return deleted
