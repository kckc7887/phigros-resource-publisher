from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from phigros_publisher import PublishPipeline, TaskState


def main() -> None:
    demo_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="验证本地 APK 到上传前的完整流程")
    parser.add_argument("--apk", required=True, help="本地 Phigros APK 路径")
    parser.add_argument("--game-version", default="", help="可选，覆盖从文件名推断的版本号")
    args = parser.parse_args()
    state = TaskState(max_logs=1000)
    state.reset()
    pipeline = PublishPipeline(demo_root, demo_root / "work", state)
    result = pipeline.run(
        {
            "mode": "local",
            "apk_path": args.apk,
            "game_version": args.game_version or None,
            "upload": False,
        }
    )
    state.complete(result)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    snapshot = state.to_dict()
    print(
        json.dumps(
            {
                "status": snapshot["status"],
                "message": snapshot["message"],
                "game_version": result["game_version"],
                "apk_path": result["apk_path"],
                "release": result["release"],
                "upload": result["upload"],
                "logs_tail": snapshot["logs"][-10:],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
