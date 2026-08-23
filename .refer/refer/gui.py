from __future__ import annotations

import json
import tkinter as tk
from threading import Thread
from tkinter import filedialog, messagebox, ttk
from typing import Any

from phigros_publisher import PublishPipeline, TaskState
from phigros_publisher.config_store import load_config, save_config
from phigros_publisher.paths import resource_root, writable_root
from phigros_publisher.pipeline import ACTIONS
from phigros_publisher.uploader import UPLOAD_SCOPES

ACTION_HINTS = {
    "parse": "解包整理到 work/latest，不连接对象存储。",
    "full": "解包整理后，按所选范围上传到对象存储。",
    "upload": "不重新解包；直接上传 work/latest 中已有结果的指定内容。",
}

MODE_HINTS = {
    "local": "选择本机已有 APK。",
    "live": "从 TapTap 下载最新 APK 到 work/cache/latest-apk（只保留最后一次）。",
}

STAGES = ("resolve", "probe", "apk", "extract", "organize", "upload")


class PublisherApp:
    def __init__(self) -> None:
        self.resource_dir = resource_root()
        self.write_dir = writable_root()
        self.work_dir = self.write_dir / "work"
        self.state = TaskState()
        self.pipeline = PublishPipeline(self.resource_dir, self.work_dir, self.state)
        self.config = load_config(self.write_dir)

        self.root = tk.Tk()
        self.root.title("rRanker · Phigros 资源发布台")
        self.root.minsize(880, 640)
        self.root.geometry("980x720")

        self.action_var = tk.StringVar(value=str(self.config.get("action") or "parse"))
        self.mode_var = tk.StringVar(value=str(self.config.get("mode") or "local"))
        self.apk_var = tk.StringVar(value=str(self.config.get("apk_path") or ""))
        self.music_var = tk.BooleanVar(value=bool(self.config.get("music", False)))
        self.scope_var = tk.StringVar(value=str(self.config.get("upload_scope") or "all"))
        self.endpoint_var = tk.StringVar(value=str(self.config.get("endpoint") or ""))
        self.bucket_var = tk.StringVar(value=str(self.config.get("bucket") or ""))
        self.public_base_var = tk.StringVar(value=str(self.config.get("public_base") or ""))
        self.access_key_var = tk.StringVar(value=str(self.config.get("access_key") or ""))
        self.secret_key_var = tk.StringVar(value=str(self.config.get("secret_key") or ""))
        self.remember_var = tk.BooleanVar(value=bool(self.config.get("remember_keys", True)))
        self.status_var = tk.StringVar(value="IDLE")
        self.message_var = tk.StringVar(value="等待开始")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="0%")
        self.action_hint_var = tk.StringVar()
        self.mode_hint_var = tk.StringVar()
        self.scope_hint_var = tk.StringVar()

        self._build_ui()
        self._sync_fields()
        self.root.after(400, self._poll_status)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(header, text="Phigros 资源发布台", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        ttk.Label(
            header,
            text="仅解析、解析并上传，或只上传已有结果。密钥可写入同目录 config.json。",
        ).pack(anchor=tk.W)

        body = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(body, padding=4)
        right = ttk.Frame(body, padding=4)
        body.add(left, weight=1)
        body.add(right, weight=1)

        controls = ttk.LabelFrame(left, text="选择流程", padding=10)
        controls.pack(fill=tk.BOTH, expand=True)

        ttk.Label(controls, text="执行动作").pack(anchor=tk.W)
        self._action_keys = list(ACTIONS.keys())
        action = ttk.Combobox(
            controls,
            values=[ACTIONS[key] for key in self._action_keys],
            state="readonly",
        )
        action.set(ACTIONS.get(self.action_var.get(), ACTIONS["parse"]))
        action.pack(fill=tk.X, pady=(0, 2))

        def on_action(_event=None) -> None:
            label = action.get()
            for key, text in ACTIONS.items():
                if text == label:
                    self.action_var.set(key)
                    break
            self._sync_fields()

        action.bind("<<ComboboxSelected>>", on_action)
        self._action_combo = action
        ttk.Label(controls, textvariable=self.action_hint_var, wraplength=420).pack(anchor=tk.W, pady=(0, 8))

        self.source_frame = ttk.Frame(controls)
        self.source_frame.pack(fill=tk.X)
        ttk.Label(self.source_frame, text="APK 来源").pack(anchor=tk.W)
        self._mode_labels = {"local": "本地 APK", "live": "下载最新 APK"}
        mode = ttk.Combobox(
            self.source_frame,
            values=list(self._mode_labels.values()),
            state="readonly",
        )
        mode.set(self._mode_labels.get(self.mode_var.get(), "本地 APK"))
        mode.pack(fill=tk.X, pady=(0, 2))

        def on_mode(_event=None) -> None:
            label = mode.get()
            for key, text in self._mode_labels.items():
                if text == label:
                    self.mode_var.set(key)
                    break
            self._sync_fields()

        mode.bind("<<ComboboxSelected>>", on_mode)
        ttk.Label(self.source_frame, textvariable=self.mode_hint_var, wraplength=420).pack(anchor=tk.W, pady=(0, 8))

        self.local_frame = ttk.Frame(self.source_frame)
        self.local_frame.pack(fill=tk.X)
        ttk.Label(self.local_frame, text="本地 APK 路径").pack(anchor=tk.W)
        path_row = ttk.Frame(self.local_frame)
        path_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Entry(path_row, textvariable=self.apk_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(path_row, text="选择文件…", command=self._pick_apk).pack(side=tk.LEFT, padx=(6, 0))

        self.music_row = ttk.Frame(self.source_frame)
        self.music_row.pack(fill=tk.X)
        ttk.Checkbutton(
            self.music_row,
            text="提取音乐（music/，数百 MiB 至数 GiB）",
            variable=self.music_var,
        ).pack(anchor=tk.W)
        ttk.Label(
            self.music_row,
            text="从 APK 重建全曲 .ogg；上传范围可选「仅 music/ 目录」。",
            foreground="#808080",
        ).pack(anchor=tk.W)

        self.upload_frame = ttk.LabelFrame(controls, text="上传配置", padding=8)
        self.upload_frame.pack(fill=tk.X, pady=(4, 0))

        ttk.Label(self.upload_frame, text="上传范围").pack(anchor=tk.W)
        self._scope_keys = list(UPLOAD_SCOPES.keys())
        scope = ttk.Combobox(
            self.upload_frame,
            values=[UPLOAD_SCOPES[key] for key in self._scope_keys],
            state="readonly",
        )
        scope.set(UPLOAD_SCOPES.get(self.scope_var.get(), UPLOAD_SCOPES["all"]))
        scope.pack(fill=tk.X, pady=(0, 2))

        def on_scope(_event=None) -> None:
            label = scope.get()
            for key, text in UPLOAD_SCOPES.items():
                if text == label:
                    self.scope_var.set(key)
                    break
            self._sync_fields()

        scope.bind("<<ComboboxSelected>>", on_scope)
        ttk.Label(self.upload_frame, textvariable=self.scope_hint_var, wraplength=420).pack(anchor=tk.W, pady=(0, 6))

        for label, var in (
            ("API 端点", self.endpoint_var),
            ("Bucket", self.bucket_var),
            ("公开访问地址", self.public_base_var),
            ("Access Key", self.access_key_var),
            ("Secret Key", self.secret_key_var),
        ):
            ttk.Label(self.upload_frame, text=label).pack(anchor=tk.W)
            show = "*" if "Key" in label else None
            ttk.Entry(self.upload_frame, textvariable=var, show=show).pack(fill=tk.X, pady=(0, 6))

        ttk.Checkbutton(
            self.upload_frame,
            text="记住密钥与上传配置（写入同目录 config.json）",
            variable=self.remember_var,
        ).pack(anchor=tk.W, pady=(0, 4))

        self.start_btn = ttk.Button(controls, text="开始执行", command=self._start)
        # Packed by _sync_fields after optional sections.

        monitor = ttk.LabelFrame(right, text="任务状态", padding=10)
        monitor.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(monitor)
        top.pack(fill=tk.X)
        ttk.Label(top, textvariable=self.status_var, font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        ttk.Label(top, textvariable=self.progress_text_var).pack(side=tk.RIGHT)

        self.stage_labels: dict[str, ttk.Label] = {}
        stage_row = ttk.Frame(monitor)
        stage_row.pack(fill=tk.X, pady=8)
        for name in STAGES:
            label = ttk.Label(stage_row, text=name, padding=(6, 2))
            label.pack(side=tk.LEFT, padx=2)
            self.stage_labels[name] = label

        ttk.Progressbar(monitor, maximum=100, variable=self.progress_var).pack(fill=tk.X)
        ttk.Label(monitor, textvariable=self.message_var, wraplength=440).pack(anchor=tk.W, pady=6)

        ttk.Label(monitor, text="日志").pack(anchor=tk.W)
        self.logs = tk.Text(monitor, height=14, wrap=tk.WORD)
        self.logs.pack(fill=tk.BOTH, expand=True)
        self.logs.configure(state=tk.DISABLED)

        ttk.Label(monitor, text="结果").pack(anchor=tk.W, pady=(8, 0))
        self.result = tk.Text(monitor, height=8, wrap=tk.WORD)
        self.result.pack(fill=tk.BOTH, expand=True)
        self.result.configure(state=tk.DISABLED)

        footer = ttk.Label(
            outer,
            text=f"资源目录：{self.resource_dir}  |  工作目录：{self.work_dir}",
        )
        footer.pack(fill=tk.X, pady=(8, 0))

    def _needs_upload(self) -> bool:
        return self.action_var.get() in {"full", "upload"}

    def _sync_fields(self) -> None:
        action = self.action_var.get()
        mode = self.mode_var.get()
        scope = self.scope_var.get() or "all"
        self.action_hint_var.set(ACTION_HINTS.get(action, ""))
        self.mode_hint_var.set(MODE_HINTS.get(mode, ""))
        if scope == "all":
            self.scope_hint_var.set(
                "全量上传成功后会清空 phigros/releases/，仅保留本次上传；局部上传不会删除其他对象。"
            )
        else:
            self.scope_hint_var.set("局部上传只覆盖所选对象，不会清理桶内其他资源。")

        self.source_frame.pack_forget()
        self.upload_frame.pack_forget()
        self.start_btn.pack_forget()

        if action != "upload":
            self.source_frame.pack(fill=tk.X)
            if mode == "local":
                self.local_frame.pack(fill=tk.X)
            else:
                self.local_frame.pack_forget()

        if self._needs_upload():
            self.upload_frame.pack(fill=tk.X, pady=(4, 0))

        self.start_btn.pack(fill=tk.X, pady=(12, 0))

    def _pick_apk(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 Phigros APK",
            filetypes=[("Android APK", "*.apk"), ("All files", "*.*")],
        )
        if selected:
            self.apk_var.set(selected)

    def _set_text(self, widget: tk.Text, value: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, value)
        widget.configure(state=tk.DISABLED)
        widget.see(tk.END)

    def _persist_config(self) -> None:
        save_config(
            {
                "endpoint": self.endpoint_var.get().strip(),
                "bucket": self.bucket_var.get().strip(),
                "public_base": self.public_base_var.get().strip(),
                "access_key": self.access_key_var.get(),
                "secret_key": self.secret_key_var.get(),
                "upload_scope": self.scope_var.get() or "all",
                "remember_keys": bool(self.remember_var.get()),
                "action": self.action_var.get(),
                "mode": self.mode_var.get(),
                "apk_path": self.apk_var.get().strip(),
                "music": bool(self.music_var.get()),
            },
            self.write_dir,
        )

    def _build_options(self) -> dict[str, Any]:
        action = self.action_var.get()
        mode = self.mode_var.get()
        options: dict[str, Any] = {"action": action, "mode": mode}
        if action != "upload":
            options["music"] = bool(self.music_var.get())
        if action != "upload" and mode == "local":
            apk_path = self.apk_var.get().strip()
            if not apk_path:
                raise ValueError("请先选择或填写本地 APK 路径")
            options["apk_path"] = apk_path
        if self._needs_upload():
            options["s3"] = {
                "endpoint": self.endpoint_var.get().strip(),
                "bucket": self.bucket_var.get().strip(),
                "public_base": self.public_base_var.get().strip(),
                "access_key": self.access_key_var.get(),
                "secret_key": self.secret_key_var.get(),
                "upload_scope": self.scope_var.get() or "all",
            }
        return options

    def _start(self) -> None:
        if self.state.is_running():
            messagebox.showwarning("忙碌中", "已有任务正在运行")
            return
        try:
            options = self._build_options()
            self._persist_config()
        except ValueError as error:
            messagebox.showerror("无法开始", str(error))
            return

        self.state.reset()
        self.start_btn.configure(state=tk.DISABLED)
        self._set_text(self.result, "")

        def worker() -> None:
            try:
                result = self.pipeline.run(options)
                self.state.complete(result)
            except Exception as error:  # noqa: BLE001 — surface any pipeline failure in GUI
                self.state.fail(error)

        Thread(target=worker, daemon=True).start()

    def _poll_status(self) -> None:
        snapshot = self.state.to_dict()
        status = snapshot.get("status") or "idle"
        self.status_var.set(str(status).upper())
        self.message_var.set(snapshot.get("error") or snapshot.get("message") or "")
        progress = float(snapshot.get("progress") or 0.0)
        self.progress_var.set(progress)
        self.progress_text_var.set(f"{round(progress)}%")
        stage = snapshot.get("stage") or ""
        for name, label in self.stage_labels.items():
            label.configure(relief=tk.SOLID if name == stage else tk.FLAT)

        logs = snapshot.get("logs") or []
        self._set_text(self.logs, "\n".join(logs) if logs else "尚无日志。")

        if snapshot.get("result"):
            result = snapshot["result"]
            summary = {
                "动作": result.get("action"),
                "APK来源": result.get("mode"),
                "上传范围": result.get("upload_scope") or "（未上传）",
                "游戏版本": result.get("game_version"),
                "最新版本": (result.get("latest") or {}).get("version") or "（未查询）",
                "下载探测": (
                    f"HTTP {(result.get('download_probe') or {}).get('status')}"
                    if result.get("download_probe")
                    else "（未探测）"
                ),
                "APK": result.get("apk_path") or "（仅上传）",
                "资源数": (result.get("release") or {}).get("asset_count"),
                "物量表": (result.get("release") or {}).get("note_counts"),
                "整理目录": (result.get("release") or {}).get("release_root"),
                "上传结果": result.get("upload") or "未执行",
            }
            self._set_text(self.result, json.dumps(summary, ensure_ascii=False, indent=2))

        running = status == "running"
        self.start_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.root.after(400, self._poll_status)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    app = PublisherApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
