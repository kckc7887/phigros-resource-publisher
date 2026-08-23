from __future__ import annotations

from pathlib import Path
import shutil


def bundled_toolchain_root(demo_root: Path) -> Path:
    root = demo_root / "bundled" / "phiTool"
    script_dir = root / "script-py"
    required = (
        script_dir / "gameInformation.py",
        script_dir / "resource.py",
        script_dir / "log.py",
        script_dir / "typetree.json",
    )
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "内置解包工具链不完整，缺少："
            + ", ".join(missing)
            + f"。请确认 {root} 目录存在。"
        )
    return root


def prepare_toolchain(demo_root: Path, destination_dir: Path) -> Path:
    source = bundled_toolchain_root(demo_root)
    destination = destination_dir / "phiTool"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination
