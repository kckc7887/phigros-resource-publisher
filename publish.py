"""Phigros 资源全量发布入口（GitHub Actions 用）。

流程：TapTap 下载最新 APK → 全量解包（含全曲音乐）→ 整理发布目录 → 全量上传对象存储。
配置全部来自环境变量（由 GitHub Secrets 注入）。

必填环境变量：
    S3_BUCKET      对象存储桶名
    S3_ACCESS_KEY  Access Key
    S3_SECRET_KEY  Secret Key
可选环境变量：
    S3_ENDPOINT    S3 兼容端点（默认雨云 https://cn-nb1.rains3.com）
    S3_PUBLIC_BASE 公网访问基址（仅用于汇总中的 current_url）
    S3_UPLOAD_WORKERS 并行上传线程数，1-32（默认 8；跨境传小文件多，串行会被延迟拖垮）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

from phigros_publisher.extractor import extract_resources
from phigros_publisher.organizer import organize_release
from phigros_publisher.taptap import download_apk, get_latest_download, probe_download
from phigros_publisher.uploader import upload_release

DEFAULT_ENDPOINT = "https://cn-nb1.rains3.com"
REQUIRED_ENV = ("S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY")

# 全量语义：音乐等大文件永远提取、永远上传；上传后清空桶内旧 releases。
UPLOAD_SCOPE = "all"
DELETE_PREVIOUS = True

# 解包阶段 extractor 会把 sys.stdout 重定向到日志 writer；
# 日志回调必须写“真实 stdout”，否则会形成 writer→回调→stdout(writer) 的无限递归。
_REAL_STDOUT = sys.stdout


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


def _load_upload_workers() -> int:
    """并行上传线程数：默认 8，可经 S3_UPLOAD_WORKERS 调整（1-32）。"""
    raw = os.environ.get("S3_UPLOAD_WORKERS", "").strip()
    if not raw:
        return 8
    try:
        return max(1, min(int(raw), 32))
    except ValueError:
        raise SystemExit(f"S3_UPLOAD_WORKERS 需为 1-32 的整数，当前值：{raw!r}")


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
        "uploaded": upload.get("uploaded"),
        "deletedPrevious": upload.get("deleted_previous"),
        "currentUrl": upload.get("current_url"),
        "elapsedSeconds": round(elapsed, 1),
    }
    _reset_dir(artifacts_dir)
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

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as sink:
            sink.write("\n## Phigros 全量发布结果\n\n")
            sink.write("| 项目 | 值 |\n| --- | --- |\n")
            sink.write(f"| 游戏版本 | {version} |\n")
            sink.write(f"| 资产数量 | {release.get('asset_count')} |\n")
            sink.write(f"| 资产总大小 | {_fmt_bytes(release.get('total_bytes') or 0)} |\n")
            sink.write(f"| 上传对象数 | {upload.get('uploaded')} |\n")
            sink.write(f"| 清理旧对象数 | {upload.get('deleted_previous')} |\n")
            current_url = upload.get("current_url")
            if current_url:
                sink.write(f"| current.json | {current_url} |\n")
            sink.write(f"| 耗时 | {elapsed / 60:.1f} 分钟 |\n")


def main() -> None:
    started = time.monotonic()
    repo_root = Path(__file__).resolve().parent
    config = _load_config()
    upload_workers = _load_upload_workers()
    _log(
        f"对象存储：endpoint={config['endpoint']} bucket={config['bucket']}"
        f"（并行上传 {upload_workers} 线程）"
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
    )
    _log(
        f"整理完成：{release['asset_count']} 个资产，"
        f"共 {_fmt_bytes(release['total_bytes'])}"
    )

    # 解包中间产物已复制进 release，删除以释放磁盘。
    shutil.rmtree(work_latest / "toolchain", ignore_errors=True)
    _log("已删除解包中间产物以释放磁盘")

    # ---- 阶段 4：全量上传 ----
    _log(f"开始全量上传（{upload_workers} 线程并行，上传后清空桶内 phigros/releases/ 旧对象）")
    upload = upload_release(
        release,
        {
            **config,
            "upload_scope": UPLOAD_SCOPE,
            "delete_previous": DELETE_PREVIOUS,
            "max_workers": upload_workers,
        },
        _item_progress("上传", 10),
    )
    _log(
        f"上传完成：{upload['uploaded']} 个对象，"
        f"清理旧对象 {upload['deleted_previous']} 个"
    )

    # ---- 阶段 5：汇总与归档 ----
    elapsed = time.monotonic() - started
    # 用清洗后的版本号，与实际上传 key / manifest / current.json 保持一致。
    _write_summary(repo_root / "work" / "artifacts", release, upload, release["version"], elapsed)
    if upload.get("current_url"):
        _log(f"current.json 地址：{upload['current_url']}")
    _log(f"全量发布完成，总耗时 {elapsed / 60:.1f} 分钟")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as error:  # noqa: BLE001 - 顶层兜底，让工作流红脸并保留堆栈
        print(f"[publish] 流程执行失败：{error}", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.exit(1)
