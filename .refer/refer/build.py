# -*- coding: utf-8 -*-
"""Phigros 资源发布台一键打包脚本。

用法：
    python build.py             # 清理后打包
    python build.py --no-clean  # 跳过清理旧的 build/ dist/

依赖：PyInstaller、requirements.txt 中的运行时包。
产出：dist/PhigrosResourcePublisher.exe
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_FILE = os.path.join(HERE, "build.spec")
DIST_DIR = os.path.join(HERE, "dist")
BUILD_DIR = os.path.join(HERE, "build")
EXE_PATH = os.path.join(DIST_DIR, "PhigrosResourcePublisher.exe")


def _check_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def check_dependencies() -> None:
    missing: list[str] = []
    if not _check_module("PyInstaller"):
        missing.append("pyinstaller")
    for module, pip_name in (
        ("boto3", "boto3"),
        ("UnityPy", "UnityPy"),
        ("PIL", "Pillow"),
        ("requests", "requests"),
    ):
        if not _check_module(module):
            missing.append(pip_name)
    if missing:
        print("[ERROR] 缺少以下依赖：")
        for item in missing:
            print(f"  - {item}")
        print("请先安装：")
        print(f"    pip install {' '.join(missing)}")
        print("或：")
        print("    pip install -r requirements.txt pyinstaller")
        sys.exit(1)


def clean_build_artifacts() -> None:
    # 只清 PyInstaller 产物，保留 dist/work 与 config.json（避免误删已下 APK）。
    if os.path.isdir(BUILD_DIR):
        print(f"[INFO] 清理 {BUILD_DIR}")
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
    if os.path.isfile(EXE_PATH):
        print(f"[INFO] 删除旧 exe：{EXE_PATH}")
        try:
            os.remove(EXE_PATH)
        except OSError as error:
            print(f"[WARN] 无法删除旧 exe：{error}")
    os.makedirs(DIST_DIR, exist_ok=True)


def run_pyinstaller() -> None:
    cmd = [sys.executable, "-m", "PyInstaller", SPEC_FILE, "--noconfirm"]
    print("[INFO] 执行：", " ".join(cmd))
    result = subprocess.run(cmd, cwd=HERE)
    if result.returncode != 0:
        print("[ERROR] PyInstaller 打包失败")
        sys.exit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phigros 资源发布台打包脚本")
    parser.add_argument("--no-clean", action="store_true", help="跳过清理旧的 build/ dist/")
    args = parser.parse_args()

    check_dependencies()
    if args.no_clean:
        print("[INFO] 跳过清理（--no-clean）")
    else:
        clean_build_artifacts()

    run_pyinstaller()

    if os.path.isfile(EXE_PATH):
        print("[OK] 打包完成：")
        print(f"     {EXE_PATH}")
    else:
        print(f"[ERROR] 未找到预期产出：{EXE_PATH}")
        sys.exit(1)


if __name__ == "__main__":
    main()
