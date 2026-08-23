from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any

from .apk import parse_apk_version, validate_apk_path
from .extractor import extract_resources
from .organizer import organize_release
from .state import TaskState
from .taptap import download_apk, get_latest_download, probe_download
from .uploader import UPLOAD_SCOPES, normalize_upload_scope, upload_release

# 所有本地产物只保留最新一份。
LATEST_APK_DIR_NAME = "latest-apk"
LATEST_WORK_DIR_NAME = "latest"

ACTIONS = {
    "parse": "仅解析整理",
    "full": "解析并上传",
    "upload": "仅上传已有结果",
}


class PublishPipeline:
    def __init__(self, demo_root: Path, work_root: Path, state: TaskState) -> None:
        self.demo_root = demo_root.resolve()
        self.work_root = work_root.resolve()
        self.state = state

    def _latest_apk_dir(self) -> Path:
        return self.work_root / "cache" / LATEST_APK_DIR_NAME

    def _latest_work_dir(self) -> Path:
        return self.work_root / LATEST_WORK_DIR_NAME

    def _reset_dir(self, path: Path) -> Path:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _prepare_latest_apk_slot(self, apk_name: str) -> Path:
        """清空固定下载目录后返回本次 APK 目标路径。"""
        apk_dir = self._reset_dir(self._latest_apk_dir())
        return apk_dir / apk_name

    def _prepare_latest_work_slot(self) -> Path:
        """清空固定工作区（解包工具链 + 发布产物），并移除历史 runs 目录。"""
        legacy_runs = self.work_root / "runs"
        if legacy_runs.exists():
            shutil.rmtree(legacy_runs)
        return self._reset_dir(self._latest_work_dir())

    def load_latest_release(self) -> dict[str, Any]:
        release_root = self._latest_work_dir() / "release"
        current_path = release_root / "phigros" / "current.json"
        if not current_path.is_file():
            raise FileNotFoundError(
                f"没有可上传的最新解析结果：{current_path}。请先执行「仅解析」或「解析并上传」。"
            )
        current = json.loads(current_path.read_text(encoding="utf-8"))
        version = str(current.get("gameVersion", "")).strip()
        if not version:
            raise ValueError("current.json 缺少 gameVersion")
        version_dir = release_root / "phigros" / "releases" / version
        if not version_dir.is_dir():
            raise FileNotFoundError(f"版本目录不存在：{version_dir}")
        assets = [path for path in version_dir.rglob("*") if path.is_file()]
        return {
            "version": version,
            "version_dir": str(version_dir),
            "release_root": str(release_root),
            "current_path": str(current_path),
            "asset_count": len(assets),
            "total_bytes": sum(path.stat().st_size for path in assets),
            "current": current,
        }

    def _resolve_apk(self, options: dict[str, Any]) -> tuple[Path, str, dict[str, Any] | None, dict[str, Any] | None]:
        mode = options.get("mode", "local")
        if mode not in {"local", "live"}:
            raise ValueError("mode 必须是 local 或 live")

        if mode == "local":
            apk_path = validate_apk_path(options.get("apk_path", ""))
            game_version = parse_apk_version(apk_path, options.get("game_version"))
            self.state.update("apk", f"本地 APK：{apk_path.name}（版本 {game_version}）", 10)
            return apk_path, game_version, None, None

        self.state.update("resolve", "正在查询 TapTap 最新 Phigros 版本", 2)
        latest = get_latest_download()
        self.state.update(
            "probe",
            f"最新版本 {latest['version']}，正在验证下载地址 HTTP 200",
            5,
        )
        probe = probe_download(latest["url"])
        self.state.update("probe", "下载地址返回 HTTP 200，探测通过", 8)
        self.state.update("apk", "正在下载最新 APK（覆盖固定缓存目录）", 10)
        apk_path = self._prepare_latest_apk_slot(latest["apk_name"])

        def download_progress(done: int, total: int) -> None:
            percent = 10 + (done / total * 25 if total else 0)
            self.state.update(
                "apk",
                f"正在下载 APK：{done / 1024**2:.1f} / {total / 1024**2:.1f} MiB",
                percent,
                log=False,
            )

        download_apk(latest, str(apk_path), download_progress)
        return apk_path, latest["version"], latest, probe

    def _parse(self, options: dict[str, Any]) -> dict[str, Any]:
        work_dir = self._prepare_latest_work_slot()
        apk_path, game_version, latest, probe = self._resolve_apk(options)

        self.state.update("extract", f"开始解包 {apk_path.name}", 36)

        def extraction_log(message: str) -> None:
            self.state.update("extract", message, log=True)

        extracted = extract_resources(
            self.demo_root,
            apk_path,
            work_dir / "toolchain",
            extraction_log,
            music=bool(options.get("music", False)),
        )
        self.state.update("organize", "解包完成，正在整理目录并计算 SHA-256", 72)

        def organize_progress(done: int, total: int, name: str) -> None:
            percent = 72 + (done / total * 18 if total else 0)
            self.state.update(
                "organize",
                f"整理并校验 {done}/{total}：{name}",
                percent,
                log=False,
            )

        release = organize_release(
            extracted,
            work_dir / "release",
            game_version,
            organize_progress,
        )
        return {
            "mode": options.get("mode", "local"),
            "game_version": game_version,
            "latest": {key: latest[key] for key in ("version", "apk_name", "size")} if latest else None,
            "download_probe": probe,
            "apk_path": str(apk_path),
            "release": release,
            "work_dir": str(work_dir),
        }

    def _upload(self, release: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        scope = normalize_upload_scope((options.get("s3") or {}).get("upload_scope") or options.get("upload_scope"))
        label = UPLOAD_SCOPES[scope]
        self.state.update("upload", f"正在上传：{label}", 91)

        def upload_progress(done: int, total: int, name: str) -> None:
            self.state.update(
                "upload",
                f"上传 {done}/{total}：{name}",
                91 + (done / total * 8 if total else 0),
                log=False,
            )

        s3 = dict(options.get("s3", {}) or {})
        s3["upload_scope"] = scope
        if "delete_previous" not in s3:
            s3["delete_previous"] = scope == "all"
        return upload_release(release, s3, upload_progress)

    def run(self, options: dict[str, Any]) -> dict[str, Any]:
        action = options.get("action") or ("full" if options.get("upload") else "parse")
        if action not in ACTIONS:
            raise ValueError(f"action 必须是 {', '.join(ACTIONS)}")

        upload_result = None
        if action == "upload":
            self.state.update("ready", "读取本地最新解析结果", 20)
            release_bundle = {
                "mode": "upload",
                "game_version": None,
                "latest": None,
                "download_probe": None,
                "apk_path": None,
                "work_dir": str(self._latest_work_dir()),
            }
            release = self.load_latest_release()
            release_bundle["game_version"] = release["version"]
            release_bundle["release"] = release
            upload_result = self._upload(release, options)
            release_bundle["upload"] = upload_result
            release_bundle["action"] = action
            release_bundle["upload_scope"] = upload_result.get("scope")
            self.state.update("ready", "仅上传完成", 100)
            return release_bundle

        parsed = self._parse(options)
        parsed["action"] = action
        if action == "full":
            upload_result = self._upload(parsed["release"], options)
            parsed["upload"] = upload_result
            parsed["upload_scope"] = upload_result.get("scope")
        else:
            parsed["upload"] = None
            parsed["upload_scope"] = None
            self.state.update("ready", "仅解析整理完成，未上传", 99)
        return parsed
