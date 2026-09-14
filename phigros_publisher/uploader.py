from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable
from .organizer import validate_release
from .storage_check import verify_conditional_writes


UPLOAD_SCOPES = {
    "all": "校验清单，变化时完整发布并清理切换前的旧发布快照",
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
        "retries": {"total_max_attempts": 5, "mode": "standard"},
        "connect_timeout": 15,
        "read_timeout": 120,
        "max_pool_connections": max(8, int(config.get("max_workers") or 8)),
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


CURRENT_KEY = "phigros/current.json"
MAX_JSON_BYTES = 32 * 1024 * 1024


def _release_prefix(current: dict[str, Any]) -> str:
    key = current.get("manifest", "")
    if not isinstance(key, str) or not re.fullmatch(r"phigros/releases/[A-Za-z0-9][A-Za-z0-9._-]*/manifest\.json", key):
        raise ValueError("current 清单路径不属于独立 Phigros 发布目录")
    prefix = key.rsplit("/", 1)[0] + "/"
    for name, suffix in (("catalog", "catalog.json"), ("noteCounts", "metadata/note_counts.tsv")):
        if current.get(name, prefix + suffix if name == "noteCounts" else None) != prefix + suffix:
            raise ValueError("current 资源路径不在同一发布目录")
    return prefix


def _manifest_identity(manifest: dict[str, Any]) -> tuple:
    if (type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1
            or not isinstance(manifest.get("gameVersion"), str) or not manifest["gameVersion"].strip()):
        raise ValueError("清单版本无效")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError("资源清单为空")
    rows = []
    seen = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("资源清单条目无效")
        path, size, digest = asset.get("path"), asset.get("size"), asset.get("sha256")
        if (not isinstance(path, str) or not path or "\\" in path or ":" in path
                or any(part in ("", ".", "..") for part in path.split("/"))
                or path == "manifest.json" or path in seen
                or not isinstance(size, int) or isinstance(size, bool) or size <= 0
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(asset.get("contentType"), str)):
            raise ValueError("资源清单路径、大小或 SHA-256 无效")
        seen.add(path)
        rows.append((path, size, digest, asset["contentType"]))
    if (type(manifest.get("assetCount")) is not int or type(manifest.get("totalBytes")) is not int
            or manifest["assetCount"] != len(rows) or manifest["totalBytes"] != sum(row[1] for row in rows)):
        raise ValueError("资源清单统计不一致")
    return manifest["gameVersion"], tuple(sorted(rows))


def _is_missing(error: Exception) -> bool:
    response = getattr(error, "response", {})
    return (response.get("Error", {}).get("Code") in {"NoSuchKey", "NotFound", "404"}
            or response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404)


def _get_bytes(client: Any, bucket: str, key: str, *, missing_ok: bool = False) -> tuple[bytes, str] | None:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as error:
        if missing_ok and _is_missing(error):
            return None
        raise
    body = response["Body"]
    try:
        data = body.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise ValueError(f"远端 JSON 过大：{key}")
        if response.get("ContentLength", len(data)) != len(data):
            raise ValueError(f"远端 JSON 长度不一致：{key}")
        return data, response.get("ETag", "")
    finally:
        body.close()


def _baseline(client: Any, bucket: str) -> dict[str, Any]:
    result = {"etag": None, "current": None, "manifest": None, "prefix": None}
    remote = _get_bytes(client, bucket, CURRENT_KEY, missing_ok=True)
    if remote is None:
        return result
    raw, etag = remote
    if not isinstance(etag, str) or not etag:
        raise ValueError("现有 current 缺少 ETag，无法安全进行条件切换")
    result["etag"] = etag
    try:
        current = json.loads(raw)
        prefix = _release_prefix(current)
    except (ValueError, TypeError, AttributeError):
        return result
    result.update(current=current, prefix=prefix)
    remote_manifest = _get_bytes(client, bucket, current["manifest"], missing_ok=True)
    if remote_manifest is None:
        return result
    try:
        manifest_bytes = remote_manifest[0]
        if hashlib.sha256(manifest_bytes).hexdigest() != current.get("manifestSha256"):
            return result
        manifest = json.loads(manifest_bytes)
        manifest_text = manifest_bytes.decode("utf-8")
        _manifest_identity(manifest)
        if (type(current.get("schemaVersion")) is not int or current["schemaVersion"] != 1
                or manifest["gameVersion"] != current.get("gameVersion")
                or manifest.get("generatedAt") != current.get("publishedAt")):
            return result
    except (ValueError, TypeError, AttributeError, KeyError):
        return result
    return {"etag": etag, "current": current, "manifest": manifest, "prefix": prefix,
            "manifest_text": manifest_text}


def _snapshot(client: Any, bucket: str, prefix: str) -> list[str]:
    keys = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            if not isinstance(key, str) or not key.startswith(prefix) or key == prefix:
                raise ValueError("对象列表返回了发布快照之外的路径")
            keys.append(key)
    return sorted(set(keys))


def _save_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _verify_object(client: Any, bucket: str, key: str, size: int, digest: str) -> None:
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    count, sha = 0, hashlib.sha256()
    try:
        if response.get("ContentLength") != size:
            raise ValueError(f"远端资源大小不一致：{key}")
        while chunk := body.read(4 * 1024 * 1024):
            count += len(chunk)
            sha.update(chunk)
            if count > size:
                raise ValueError(f"远端资源超过预期大小：{key}")
        if count != size or sha.hexdigest() != digest:
            raise ValueError(f"远端资源 SHA-256 校验失败：{key}")
    finally:
        body.close()


def _parallel(items: list, workers: int, operation: Callable, completed: Callable) -> None:
    """Keep at most twice the worker count queued, and observe every failure."""
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="s3-publish") as pool:
        pending = set()
        try:
            while True:
                while len(pending) < workers * 2:
                    try:
                        item = next(iterator)
                    except StopIteration:
                        break
                    pending.add(pool.submit(operation, item))
                if not pending:
                    return
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    completed(future.result())
        except BaseException:
            for future in pending:
                future.cancel()
            raise


def _cleanup(client: Any, bucket: str, report: dict[str, Any], report_path: Path) -> None:
    remaining = list(report["cleanup_remaining"])
    prefix = report.get("previous_prefix")
    if remaining and (not isinstance(prefix, str) or not re.fullmatch(
            r"phigros/releases/[A-Za-z0-9][A-Za-z0-9._-]*/", prefix)
            or prefix == report.get("candidate_prefix")
            or any(not key.startswith(prefix) or key == prefix for key in remaining)):
        raise ValueError("拒绝删除发布快照之外的对象")
    while remaining:
        _assert_active(client, bucket, report)
        batch = remaining[:1000]
        response = client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": False})
        errors = response.get("Errors", [])
        deleted = {item.get("Key") for item in response.get("Deleted", [])}
        # S3 DeleteObjects can return HTTP 200 with individual object errors.
        failed = {item.get("Key") for item in errors}
        confirmed = set(batch) & deleted - failed
        report["deleted_previous"] += len(confirmed)
        remaining = [key for key in remaining if key not in confirmed]
        report["cleanup_remaining"] = remaining
        _save_report(report_path, report)
        if failed or len(confirmed) != len(batch):
            raise RuntimeError(f"旧发布清理未完成，剩余 {len(remaining)} 个对象；使用报告重试")
    _assert_active(client, bucket, report)


def _assert_active(client: Any, bucket: str, report: dict[str, Any]) -> None:
    active = _get_bytes(client, bucket, CURRENT_KEY)
    if (active is None or hashlib.sha256(active[0]).hexdigest() != report.get("candidate_current_sha256")
            or _release_prefix(json.loads(active[0])) != report.get("candidate_prefix")):
        raise ValueError("current 已改变，停止旧资源清理")


def _assert_conditional_support(client: Any) -> None:
    members = client.meta.service_model.operation_model("PutObject").input_shape.members
    if not {"IfMatch", "IfNoneMatch"}.issubset(members):
        raise RuntimeError("S3 SDK 缺少条件写入支持，请升级 boto3；禁止无条件切换")


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
    if scope == "current":
        raise ValueError("仅更新 current.json 无法确认远端资源完整性，请使用全量上传")
    validate_release(release_result, workers=int(config.get("parse_workers") or 4))
    version_dir = Path(release_result["version_dir"])
    current_bytes = Path(release_result["current_path"]).read_bytes()
    current = json.loads(current_bytes)
    prefix = _release_prefix(current)
    manifest_path = version_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    identity = _manifest_identity(manifest)
    workers = int(config.get("max_workers") or 8)
    if workers < 1 or workers > 32:
        raise ValueError("上传并行数必须为 1-32")
    report_path = Path(config.get("report_path") or Path(__file__).resolve().parents[1] / "work/artifacts/publication.json")
    report = {
        "schemaVersion": 1, "scope": scope, "status": "preparing", "bucket": config["bucket"],
        "endpoint": config["endpoint"].rstrip("/"), "candidate_prefix": prefix,
        "candidate_current_sha256": hashlib.sha256(current_bytes).hexdigest(),
        "previous_prefix": None, "cleanup_remaining": [], "previous_keys": [],
        "uploaded": 0, "verified": 0, "uploaded_bytes": 0, "deleted_previous": 0,
        "keys": [], "unchanged": False, "pointer_verified": False, "commit_attempted": False,
        "conditional_write_verified": False,
        "current_url": (str(config.get("public_base", "")).rstrip("/") + "/" + CURRENT_KEY)
                       if config.get("public_base") else None,
    }
    _save_report(report_path, report)
    try:
        boto3, Config = _load_boto3()
        client = _make_s3_client(boto3, Config, {**config, "max_workers": workers})
        previous = _baseline(client, config["bucket"])
        report["baseline_etag"] = previous["etag"]
        report["previous_prefix"] = previous["prefix"]
        if scope == "all" and previous["manifest"] is not None and identity == _manifest_identity(previous["manifest"]):
            report.update(status="unchanged", unchanged=True, active_current=previous["current"],
                          active_manifest_text=previous["manifest_text"])
            _save_report(report_path, report)
            return report
        if prefix == previous["prefix"]:
            raise ValueError("候选发布不得覆盖正在使用的资源目录")
        if scope == "all":
            _assert_conditional_support(client)
        # Fresh namespace is a hard precondition, including candidates from partial uploads.
        if _snapshot(client, config["bucket"], prefix):
            raise ValueError("候选发布目录已存在，请重新整理生成独立修订目录")
        if scope == "all":
            report["status"] = "checking_storage"
            _save_report(report_path, report)
            verify_conditional_writes(client, config["bucket"], "phigros")
            report["conditional_write_verified"] = True
        if scope == "all" and previous["prefix"]:
            report["previous_keys"] = _snapshot(client, config["bucket"], previous["prefix"])
        report["status"] = "uploading"
        _save_report(report_path, report)
        from boto3.s3.transfer import TransferConfig
        transfer = TransferConfig(use_threads=False, max_concurrency=1)
        asset_map = {asset["path"]: asset for asset in manifest["assets"]}
        selected = [(path, relative) for path, relative in iter_upload_files(version_dir, scope) if relative != "manifest.json"]
        total = len(selected) + (2 if scope == "all" else int(scope == "manifest"))
        if not total:
            raise ValueError(f"上传范围 {scope} 没有匹配到任何本地文件")

        def upload_one(item: tuple[Path, str]) -> tuple[str, int]:
            path, relative = item
            asset = asset_map[relative]
            key = prefix + relative
            client.upload_file(str(path), config["bucket"], key,
                               ExtraArgs={"ContentType": asset["contentType"],
                                          "CacheControl": "public, max-age=31536000, immutable",
                                          "Metadata": {"sha256": asset["sha256"]}}, Config=transfer)
            _verify_object(client, config["bucket"], key, asset["size"], asset["sha256"])
            return key, asset["size"]

        def completed(item: tuple[str, int]) -> None:
            key, size = item
            report["keys"].append(key)
            report["uploaded"] += 1
            report["verified"] += 1
            report["uploaded_bytes"] += size
            if progress:
                progress(report["uploaded"], total, key)

        _parallel(selected, workers, upload_one, completed)
        if scope in {"all", "manifest"}:
            client.put_object(Bucket=config["bucket"], Key=current["manifest"], Body=manifest_bytes,
                              ContentType="application/json", CacheControl="public, max-age=31536000, immutable",
                              ContentMD5=base64.b64encode(hashlib.md5(manifest_bytes).digest()).decode("ascii"))
            _verify_object(client, config["bucket"], current["manifest"], len(manifest_bytes), current["manifestSha256"])
            completed((current["manifest"], len(manifest_bytes)))
        if scope != "all":
            report["status"] = "partial"
            _save_report(report_path, report)
            return report
        report["status"] = "committing"
        report["commit_attempted"] = True
        _save_report(report_path, report)
        condition = {"IfMatch": previous["etag"]} if previous["etag"] else {"IfNoneMatch": "*"}
        client.put_object(Bucket=config["bucket"], Key=CURRENT_KEY, Body=current_bytes,
                          ContentType="application/json", CacheControl="no-cache, max-age=0",
                          ContentMD5=base64.b64encode(hashlib.md5(current_bytes).digest()).decode("ascii"), **condition)
        actual = _get_bytes(client, config["bucket"], CURRENT_KEY)
        if actual is None or actual[0] != current_bytes:
            raise RuntimeError("发布指针回读不一致，停止清理旧资源")
        report["pointer_verified"] = True
        completed((CURRENT_KEY, len(current_bytes)))
        report["status"] = "cleaning"
        report["cleanup_remaining"] = list(report["previous_keys"]) if config.get("delete_previous", True) else []
        _save_report(report_path, report)
        _cleanup(client, config["bucket"], report, report_path)
        report["status"] = "published"
        _save_report(report_path, report)
        return report
    except BaseException as error:
        report["status"] = "cleanup_failed" if report["pointer_verified"] else "failed"
        report["error"] = str(error)
        _save_report(report_path, report)
        raise


def retry_cleanup(report_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Retry only keys already captured before a verified pointer switch."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (not report.get("pointer_verified") or report.get("bucket") != config["bucket"]
            or report.get("endpoint") != config["endpoint"].rstrip("/")):
        raise ValueError("报告尚未确认切换成功，或报告与当前 S3 配置不一致")
    remaining, snapshot = report.get("cleanup_remaining"), report.get("previous_keys")
    if (not isinstance(remaining, list) or not isinstance(snapshot, list)
            or any(not isinstance(key, str) for key in remaining + snapshot)
            or not set(remaining).issubset(set(snapshot))):
        raise ValueError("清理报告不属于原始发布快照")
    boto3, Config = _load_boto3()
    client = _make_s3_client(boto3, Config, config)
    active = _get_bytes(client, config["bucket"], CURRENT_KEY)
    if active is None or hashlib.sha256(active[0]).hexdigest() != report.get("candidate_current_sha256"):
        raise ValueError("current 已改变，拒绝使用过期报告自动清理")
    if _release_prefix(json.loads(active[0])) != report.get("candidate_prefix"):
        raise ValueError("清理报告的候选目录与 current 不一致")
    try:
        _cleanup(client, config["bucket"], report, report_path)
        report["status"] = "published"
        report.pop("error", None)
        _save_report(report_path, report)
        return report
    except BaseException as error:
        report.update(status="cleanup_failed", error=str(error))
        _save_report(report_path, report)
        raise


def main() -> None:
    import argparse
    from publish import _load_config
    parser = argparse.ArgumentParser(description="从失败报告精准重试旧 Phigros 发布清理")
    parser.add_argument("--retry-cleanup", type=Path, required=True)
    args = parser.parse_args()
    result = retry_cleanup(args.retry_cleanup, _load_config())
    print(json.dumps({"status": result["status"], "remaining": len(result["cleanup_remaining"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
