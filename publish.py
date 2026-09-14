"""Phigros 资源全量发布入口（GitHub Actions 用）。

流程：TapTap 下载最新 APK → 并行全量解析 → 比较资源清单 → 变化时按日期目录差量发布并校验切换。
配置全部来自环境变量（由 GitHub Secrets 注入）。

必填环境变量：
    S3_BUCKET      对象存储桶名
    S3_ACCESS_KEY  Access Key
    S3_SECRET_KEY  Secret Key
可选环境变量：
    S3_ENDPOINT    S3 兼容端点（默认雨云 https://cn-nb1.rains3.com）
    S3_PUBLIC_BASE 公网访问基址（仅用于汇总中的 current_url）
    S3_UPLOAD_WORKERS 并行上传/复制线程数，1-32（默认 4）
    PHIGROS_PARSE_WORKERS 并行解析线程数，1-16（默认 4）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from phigros_publisher.extractor import extract_resources
from phigros_publisher.organizer import organize_release
from phigros_publisher.taptap import download_apk, get_latest_download, probe_download
from phigros_publisher.uploader import upload_release

DEFAULT_ENDPOINT = "https://cn-nb1.rains3.com"
REQUIRED_ENV = ("S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY")

# 全量解析；资源清单变化时按日期目录差量发布。切换后清理非当前 releases 前缀。
UPLOAD_SCOPE = "all"
DELETE_PREVIOUS = True

# 解包阶段 extractor 会把 sys.stdout 重定向到日志 writer；
# 日志回调必须写“真实 stdout”，否则会形成 writer→回调→stdout(writer) 的无限递归。
_REAL_STDOUT = sys.stdout
_RUN_ARTIFACTS_DIR: Path | None = None


def _log(message: str) -> None:
    print(f"[publish] {message}", file=_REAL_STDOUT, flush=True)


def _load_config() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        raise SystemExit(
            "缺少必需的环境变量：" + ", ".join(missing)
            + "。请在 GitHub 仓库 Settings → Secrets and variables → Actions 中配置后再运行。"
        )
    return {
        "endpoint": os.environ.get("S3_ENDPOINT", "").strip() or DEFAULT_ENDPOINT,
        "bucket": os.environ["S3_BUCKET"].strip(),
        "access_key": os.environ["S3_ACCESS_KEY"].strip(),
        "secret_key": os.environ["S3_SECRET_KEY"].strip(),
        "public_base": os.environ.get("S3_PUBLIC_BASE", "").strip(),
    }


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _load_workers(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} 需为 1-{maximum} 的整数，当前值：{raw!r}")
    if not 1 <= value <= maximum:
        raise SystemExit(f"{name} 需为 1-{maximum} 的整数，当前值：{raw!r}")
    return value


def _load_upload_workers() -> int:
    return _load_workers("S3_UPLOAD_WORKERS", 4, 32)


def _download_progress() -> Callable[[int, int], None]:
    """每 64 MiB 或每 10% 输出一次下载进度。"""
    state = {"last_mib": -1, "last_percent": -10.0}

    def report(done: int, total: int) -> None:
        done_mib = done / 1024**2
        percent = done / total * 100 if total else 100.0
        if done_mib - state["last_mib"] >= 64 or percent - state["last_percent"] >= 10 or done >= total:
            state["last_mib"] = done_mib
            state["last_percent"] = percent
            total_mib = total / 1024**2 if total else 0
            _log(f"下载 APK：{done_mib:.1f} / {total_mib:.1f} MiB（{percent:.1f}%）")

    return report


def _item_progress(label: str, every: int) -> Callable[[int, int, str], None]:
    """按条目数节流输出进度（organize / upload 共用）。"""
    state = {"last": 0, "start": time.monotonic()}

    def report(done: int, total: int, name: str) -> None:
        if done >= total or done - state["last"] >= every:
            state["last"] = done
            elapsed = time.monotonic() - state["start"]
            speed = done / elapsed if elapsed > 0 else 0.0
            percent = done / total * 100 if total else 100.0
            _log(f"{label} {done}/{total}（{percent:.1f}%，{speed:.1f} 项/秒）：{name}")

    return report


def _fmt_bytes(size: float) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GiB"


def _write_summary(
    artifacts_dir: Path,
    release: dict,
    upload: dict,
    version: str,
    elapsed: float,
) -> None:
    summary = {
        "gameVersion": version,
        "assetCount": release.get("asset_count"),
        "totalBytes": release.get("total_bytes"),
        **release.get("validation", {}),
        "uploaded": upload.get("uploaded"),
        "status": upload.get("status"),
        "unchanged": upload.get("unchanged"),
        "verified": upload.get("verified"),
        "uploadedBytes": upload.get("uploaded_bytes"),
        "cleanupRemaining": len(upload.get("cleanup_remaining", [])),
        "deletedPrevious": upload.get("deleted_previous"),
        "currentUrl": upload.get("current_url"),
        "elapsedSeconds": round(elapsed, 1),
    }
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    version_dir = Path(release["version_dir"])
    current_path = Path(release["current_path"])
    for source, target_name in (
        (current_path, "current.json"),
        (version_dir / "manifest.json", "manifest.json"),
        (version_dir / "catalog.json", "catalog.json"),
        (version_dir / "metadata" / "note_counts.tsv", "note_counts.tsv"),
    ):
        if source.is_file():
            shutil.copy2(source, artifacts_dir / target_name)
    if upload.get("active_current"):
        (artifacts_dir / "candidate-current.json").write_bytes(current_path.read_bytes())
        (artifacts_dir / "candidate-manifest.json").write_bytes((version_dir / "manifest.json").read_bytes())
        (artifacts_dir / "current.json").write_text(
            json.dumps(upload["active_current"], ensure_ascii=False, indent=2), encoding="utf-8")
        (artifacts_dir / "manifest.json").write_bytes(upload["active_manifest_text"].encode("utf-8"))

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as sink:
            sink.write("\n## Phigros 资源发布结果\n\n")
            sink.write("| 项目 | 值 |\n| --- | --- |\n")
            sink.write(f"| 游戏版本 | {version} |\n")
            sink.write(f"| 发布状态 | {upload.get('status')} |\n")
            sink.write(f"| 歌曲 / 音乐 | {release['validation']['songCount']} / {release['validation']['musicCount']} |\n")
            sink.write(f"| 资产数量 | {release.get('asset_count')} |\n")
            sink.write(f"| 资产总大小 | {_fmt_bytes(release.get('total_bytes') or 0)} |\n")
            sink.write(f"| 上传对象数 | {upload.get('uploaded')} |\n")
            sink.write(f"| 清理旧对象数 | {upload.get('deleted_previous')} |\n")
            current_url = upload.get("current_url")
            if current_url:
                sink.write(f"| current.json | {current_url} |\n")
            sink.write(f"| 耗时 | {elapsed / 60:.1f} 分钟 |\n")


def main() -> None:
    global _RUN_ARTIFACTS_DIR
    started = time.monotonic()
    repo_root = Path(__file__).resolve().parent
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    artifacts_dir = repo_root / "work" / "artifacts" / run_id
    _RUN_ARTIFACTS_DIR = artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "run-status.json").write_text('{"status":"running"}', encoding="utf-8")
    config = _load_config()
    upload_workers = _load_upload_workers()
    parse_workers = _load_workers("PHIGROS_PARSE_WORKERS", 4, 16)
    _log(
        f"对象存储：endpoint={config['endpoint']} bucket={config['bucket']}"
        f"（并行解析 {parse_workers}，并行上传 {upload_workers}）"
    )

    # ---- 阶段 1：查询并下载最新 APK ----
    _log("正在查询 TapTap 最新 Phigros 版本")
    latest = get_latest_download()
    _log(
        f"最新版本 {latest['version']}，"
        f"APK {latest['apk_name']}（{_fmt_bytes(latest['size'])}）"
    )
    probe_download(latest["url"])
    _log("下载地址返回 HTTP 200，探测通过")

    apk_dir = repo_root / "work" / "cache" / "latest-apk"
    _reset_dir(apk_dir)
    apk_path = apk_dir / latest["apk_name"]
    download_apk(latest, str(apk_path), _download_progress())
    _log("APK 下载完成")

    # ---- 阶段 2：全量解包（含全曲音乐）----
    work_latest = repo_root / "work" / "latest"
    _reset_dir(work_latest)
    _log(f"开始全量解包（含音乐）：{apk_path.name}")
    extracted = extract_resources(
        repo_root,
        apk_path,
        work_latest / "toolchain",
        log=_log,
        music=True,
        workers=parse_workers,
    )
    _log("解包完成，校验通过（avatar / chart / illustration×3 / metadata / music）")

    # APK 已用完，删除以释放磁盘（约 1 GiB 以上）。
    apk_path.unlink(missing_ok=True)
    _log("已删除 APK 缓存以释放磁盘")

    # ---- 阶段 3：整理发布目录 ----
    _log("正在整理目录并计算 SHA-256")
    release = organize_release(
        extracted,
        work_latest / "release",
        latest["version"],
        _item_progress("整理", 50),
        workers=parse_workers,
    )
    _log(
        f"整理完成：{release['asset_count']} 个资产，"
        f"共 {_fmt_bytes(release['total_bytes'])}"
    )

    # 解包中间产物已复制进 release，删除以释放磁盘。
    shutil.rmtree(work_latest / "toolchain", ignore_errors=True)
    _log("已删除解包中间产物以释放磁盘")

    # ---- 阶段 4：全量上传 ----
    _log(f"开始比较 S3 清单；变化时使用 {upload_workers} 个并行任务差量复制或上传")
    try:
        upload = upload_release(
            release,
            {
                **config,
                "upload_scope": UPLOAD_SCOPE,
                "delete_previous": DELETE_PREVIOUS,
                "max_workers": upload_workers,
                "parse_workers": parse_workers,
                "report_path": artifacts_dir / "publication.json",
            },
            _item_progress("上传", 10),
        )
    except BaseException:
        report_path = artifacts_dir / "publication.json"
        upload = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {"status": "failed"}
        _write_summary(artifacts_dir, release, upload, release["version"], time.monotonic() - started)
        raise
    _log(
        f"上传完成：{upload['uploaded']} 个对象，"
        f"清理旧对象 {upload['deleted_previous']} 个"
    )

    # ---- 阶段 5：汇总与归档 ----
    elapsed = time.monotonic() - started
    # 用清洗后的版本号，与实际上传 key / manifest / current.json 保持一致。
    _write_summary(artifacts_dir, release, upload, release["version"], elapsed)
    if upload.get("current_url"):
        _log(f"current.json 地址：{upload['current_url']}")
    (artifacts_dir / "run-status.json").write_text(
        json.dumps({"status": upload["status"]}, ensure_ascii=False), encoding="utf-8")
    _log(f"资源发布结束（{upload['status']}），总耗时 {elapsed / 60:.1f} 分钟")


def _record_failure(error: BaseException) -> None:
    artifacts_dir = _RUN_ARTIFACTS_DIR or Path(__file__).resolve().parent / "work" / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, indent=2)
    (artifacts_dir / "run-status.json").write_text(payload, encoding="utf-8")
    if not (artifacts_dir / "summary.json").is_file():
        (artifacts_dir / "summary.json").write_text(payload, encoding="utf-8")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as sink:
            sink.write("\nPhigros 发布失败，详情及可重试清理报告已保留在本次 artifact 中。\n")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as error:
        if error.code:
            _record_failure(error)
        raise
    except BaseException as error:  # noqa: BLE001 - 顶层兜底，让工作流红脸并保留堆栈
        _record_failure(error)
        print(f"[publish] 流程执行失败：{error}", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.exit(1)
