# phiTool - Phigros 数据管理工具
# Copyright (C) 2026 Chnynnya
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import argparse
import asyncio
import os
import sys
import urllib.request
import glob
import json

DEFAULT_APK_PATHS = [
    "../phigros/*.apk",
    "../Phigros_Resource/phigros/*.apk",
    "../Phigros_Resource/*.apk",
    "../../phigros/*.apk",
    "phigros/*.apk",
    "*.apk"
]

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
SAVE_DIR = os.path.join(OUTPUT_DIR, "save")
TEMP_DIR = os.path.join(OUTPUT_DIR, "temp")
METADATA_DIR = os.path.join(OUTPUT_DIR, "metadata")
AVATAR_DIR = os.path.join(OUTPUT_DIR, "avatar")
CHART_DIR = os.path.join(OUTPUT_DIR, "chart")
ILLUSTRATION_DIR = os.path.join(OUTPUT_DIR, "illustration")
ILLUSTRATION_BLUR_DIR = os.path.join(OUTPUT_DIR, "illustrationBlur")
ILLUSTRATION_LOWRES_DIR = os.path.join(OUTPUT_DIR, "illustrationLowRes")
MUSIC_DIR = os.path.join(OUTPUT_DIR, "music")

class Logger:
    def __init__(self, silent=False, json_output=False):
        self.silent = silent
        self.json_output = json_output
    
    def info(self, message):
        if not self.silent:
            sys.stderr.write(f"[*] {message}\n")
    
    def success(self, message):
        if not self.silent:
            sys.stderr.write(f"[+] {message}\n")
    
    def error(self, message):
        sys.stderr.write(f"[-] {message}\n")
    
    def debug(self, message):
        if not self.silent:
            sys.stderr.write(f"[DEBUG] {message}\n")

def ensure_output_dirs(config=None):
    os.makedirs(SAVE_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(METADATA_DIR, exist_ok=True)
    
    if config is None:
        os.makedirs(AVATAR_DIR, exist_ok=True)
        os.makedirs(CHART_DIR, exist_ok=True)
        os.makedirs(ILLUSTRATION_DIR, exist_ok=True)
        os.makedirs(ILLUSTRATION_BLUR_DIR, exist_ok=True)
        os.makedirs(ILLUSTRATION_LOWRES_DIR, exist_ok=True)
        os.makedirs(MUSIC_DIR, exist_ok=True)
    else:
        if config.get("avatar", True):
            os.makedirs(AVATAR_DIR, exist_ok=True)
        if config.get("chart", True):
            os.makedirs(CHART_DIR, exist_ok=True)
        if config.get("illustration", True):
            os.makedirs(ILLUSTRATION_DIR, exist_ok=True)
        if config.get("illustrationBlur", True):
            os.makedirs(ILLUSTRATION_BLUR_DIR, exist_ok=True)
        if config.get("illustrationLowRes", True):
            os.makedirs(ILLUSTRATION_LOWRES_DIR, exist_ok=True)
        if config.get("music", True):
            os.makedirs(MUSIC_DIR, exist_ok=True)

def find_apk():
    for pattern in DEFAULT_APK_PATHS:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None

def download_apk(logger):
    from taptap import get_download_url
    import requests
    
    logger.info("获取下载链接...")
    info = get_download_url()
    
    if logger.json_output:
        print(json.dumps({
            "level": "INFO",
            "message": "下载信息",
            "version": info.get("version"),
            "apk_name": info.get("apk_name"),
            "size_mb": info.get("size", 0) / (1024 * 1024)
        }))
    else:
        logger.info(f"版本: {info['version']}")
        logger.info(f"文件: {info['apk_name']}")
        logger.info(f"大小: {info['size'] / (1024 * 1024):.2f} MB")
    
    save_path = f"phigros/{info['apk_name']}"
    os.makedirs("phigros", exist_ok=True)
    
    logger.info("开始下载...")
    try:
        response = requests.get(info["url"], stream=True, timeout=300)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', info['size']))
        downloaded_size = 0
        chunk_size = 1024 * 1024 * 10
        
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)
                    percent = min(downloaded_size / total_size * 100, 100)
                    if not logger.silent and not logger.json_output:
                        print(f"\r    进度: {percent:.1f}% ({downloaded_size / (1024 * 1024):.2f} / {total_size / (1024 * 1024):.2f} MB)", end="")
        
        if not logger.silent and not logger.json_output:
            print("")
        logger.success("下载完成")
        return save_path
    except Exception as e:
        if not logger.silent and not logger.json_output:
            print("")
        logger.error(f"下载失败: {e}")
        if os.path.exists(save_path):
            os.remove(save_path)
        return None

def get_apk_path(apk_path, logger):
    if apk_path and os.path.exists(apk_path):
        return apk_path
    
    if not apk_path:
        logger.info("未指定APK路径，自动搜索...")
        found = find_apk()
        if found:
            logger.info(f"找到: {found}")
            return found
    
    logger.info("未找到APK文件，尝试从TapTap下载...")
    downloaded = download_apk(logger)
    if downloaded:
        return downloaded
    
    logger.error("无法获取APK文件")
    return None

def extract_metadata(apk_path, logger, config=None):
    from gameInformation import run
    from log import init_console_logger
    
    if config is None:
        config = {
            "avatar": True,
            "chart": False,
            "illustrationBlur": True,
            "illustrationLowRes": True,
            "illustration": True,
            "music": False
        }
    
    ensure_output_dirs(config)
    internal_logger = init_console_logger()
    run(apk_path, internal_logger, METADATA_DIR)

def extract_resources(apk_path, logger, config=None):
    import importlib.util
    resource_path = os.path.join(os.path.dirname(__file__), "resource.py")
    spec = importlib.util.spec_from_file_location("phi_resource", resource_path)
    resource_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resource_module)
    resource_run = resource_module.run

    from log import init_console_logger
    
    internal_logger = init_console_logger()
    
    if config is None:
        config = {
            "avatar": True,
            "chart": False,
            "illustrationBlur": True,
            "illustrationLowRes": True,
            "illustration": True,
            "music": False,
            "UPDATE": {
                "main_story": 0,
                "side_story": 0,
                "other_song": 0
            }
        }
    
    ensure_output_dirs(config)
    
    output_dirs = {
        "avatar": AVATAR_DIR,
        "chart": CHART_DIR,
        "illustrationBlur": ILLUSTRATION_BLUR_DIR,
        "illustrationLowRes": ILLUSTRATION_LOWRES_DIR,
        "illustration": ILLUSTRATION_DIR,
        "music": MUSIC_DIR
    }
    
    resource_run(apk_path, config, internal_logger, METADATA_DIR, output_dirs)

def get_taptap_url(logger):
    from taptap import get_download_url
    info = get_download_url()
    print(json.dumps({
        "version": info.get("version"),
        "apk_name": info.get("apk_name"),
        "url": info.get("url"),
        "size": info.get("size"),
        "md5": info.get("md5")
    }))

def get_save(session_token):
    from PhigrosLibrary import B19Class
    import aiohttp
    
    async def fetch_save():
        async with aiohttp.ClientSession() as client:
            b19Class = B19Class(client)
            summary = await b19Class.get_summary(session_token)
            save_url = summary["url"]
            async with client.get(save_url) as response:
                result = await response.read()
            return result
    
    save_data = asyncio.run(fetch_save())
    return save_data

def decrypt_save_to_json(save_data):
    from PhigrosLibrary import Save
    
    with Save(save_data) as save:
        result = {}
        for key in save.keys():
            result[key] = save[key]
        return json.dumps(result, ensure_ascii=False, indent=2)

def get_summary(session_token):
    from PhigrosLibrary import B19Class
    import aiohttp
    
    async def fetch_summary():
        async with aiohttp.ClientSession() as client:
            b19Class = B19Class(client)
            summary = await b19Class.get_summary(session_token)
            return summary
    
    return asyncio.run(fetch_summary())

def get_player_id(session_token):
    from PhigrosLibrary import B19Class
    import aiohttp
    
    async def fetch_player_id():
        async with aiohttp.ClientSession() as client:
            b19Class = B19Class(client)
            player_id = await b19Class.get_playerId(session_token)
            return player_id
    
    return asyncio.run(fetch_player_id())

def get_b27(session_token):
    from PhigrosLibrary import B19Class
    import aiohttp
    
    async def fetch_b27():
        async with aiohttp.ClientSession() as client:
            b19Class = B19Class(client)
            b19Class.read_difficulty("../difficulty.tsv")
            summary = await b19Class.get_summary(session_token)
            b27 = await b19Class.get_b27(summary["url"])
            return b27
    
    return asyncio.run(fetch_b27())

def main():
    parser = argparse.ArgumentParser(description="PhiTool - Phigros 多功能工具")
    
    parser.add_argument("--silent", "-s", action="store_true", help="静默模式，仅输出结果")
    parser.add_argument("--json", "-j", action="store_true", help="JSON格式输出，方便脚本调用")
    
    subparsers = parser.add_subparsers(dest="command", help="可用命令")
    
    extract_parser = subparsers.add_parser("extract", help="从APK提取资源")
    extract_parser.add_argument("apk_path", nargs="?", default=None, help="APK文件路径（可选，自动搜索或下载）")
    extract_parser.add_argument("--metadata", action="store_true", help="仅提取元数据")
    extract_parser.add_argument("--resources", action="store_true", help="仅提取媒体资源")
    
    taptap_parser = subparsers.add_parser("taptap", help="获取TapTap下载链接")
    
    save_parser = subparsers.add_parser("save", help="获取存档数据")
    save_parser.add_argument("session_token", help="Session Token")
    save_parser.add_argument("--output", "-o", help="输出文件路径")
    save_parser.add_argument("--json", action="store_true", help="保存为解密后的JSON格式")
    
    summary_parser = subparsers.add_parser("summary", help="获取存档摘要")
    summary_parser.add_argument("session_token", help="Session Token")
    
    player_parser = subparsers.add_parser("player", help="获取玩家昵称")
    player_parser.add_argument("session_token", help="Session Token")
    
    b27_parser = subparsers.add_parser("b27", help="计算B27分数")
    b27_parser.add_argument("session_token", help="Session Token")
    
    login_parser = subparsers.add_parser("login", help="通过TapTap QR码登录获取sessionToken")
    login_parser.add_argument("--token-only", action="store_true", help="仅输出sessionToken")
    
    recommend_parser = subparsers.add_parser("recommend", help="推分推荐")
    recommend_parser.add_argument("session_token", nargs="?", default=None, help="Session Token")
    recommend_parser.add_argument("--save-file", help="本地存档文件路径")
    recommend_parser.add_argument("--b27-file", help="B27结果文件路径")
    recommend_parser.add_argument("--target", type=float, default=0.01, help="目标RKS提升值（默认0.01）")
    recommend_parser.add_argument("--top", type=int, default=15, help="推荐数量（默认15）")
    
    update_parser = subparsers.add_parser("update-difficulty", help="更新难度定数表")
    update_parser.add_argument("apk_path", nargs="?", default=None, help="APK文件路径（可选，自动搜索或下载）")
    
    args = parser.parse_args()
    
    logger = Logger(silent=args.silent, json_output=args.json)
    
    if args.command == "extract":
        apk_path = get_apk_path(args.apk_path, logger)
        if not apk_path:
            logger.error("无法获取APK文件，退出")
            sys.exit(1)
        
        if not args.silent:
            logger.info(f"使用APK: {apk_path}")
        
        if args.metadata or not args.resources:
            logger.info("提取元数据...")
            extract_metadata(apk_path, logger)
        if args.resources or not args.metadata:
            logger.info("提取媒体资源...")
            extract_resources(apk_path, logger)
        logger.success("提取完成")
    
    elif args.command == "taptap":
        get_taptap_url(logger)
    
    elif args.command == "save":
        ensure_output_dirs()
        session_token = args.session_token
        
        logger.info("获取存档数据...")
        save_data = get_save(session_token)
        
        encrypted_path = os.path.join(TEMP_DIR, f"{session_token}.dat")
        decrypted_path = os.path.join(SAVE_DIR, f"{session_token}.json")
        
        with open(encrypted_path, "wb") as f:
            f.write(save_data)
        logger.info(f"加密存档已保存到 {encrypted_path}")
        
        logger.info("解密存档...")
        json_data = decrypt_save_to_json(save_data)
        
        with open(decrypted_path, "w", encoding="utf-8") as f:
            f.write(json_data)
        logger.info(f"解密存档已保存到 {decrypted_path}")
        
        print(json.dumps({
            "status": "success",
            "encrypted_path": encrypted_path,
            "decrypted_path": decrypted_path,
            "session_token": session_token
        }))
    
    elif args.command == "summary":
        summary = get_summary(args.session_token)
        print(json.dumps(summary, ensure_ascii=False))
    
    elif args.command == "player":
        player_id = get_player_id(args.session_token)
        print(json.dumps({"player_id": player_id}, ensure_ascii=False))
    
    elif args.command == "b27":
        b27 = get_b27(args.session_token)
        print(json.dumps(b27, ensure_ascii=False))
    
    elif args.command == "login":
        from taptap_login import taptap_qr_login
        asyncio.run(taptap_qr_login())
    
    elif args.command == "recommend":
        from recommend import find_recommendations
        
        if not args.session_token:
            logger.error("请提供 session_token 参数")
            sys.exit(1)
        
        logger.info("获取云存档...")
        save_raw = get_save(args.session_token)
        save_data = json.loads(decrypt_save_to_json(save_raw))
        
        logger.info("计算推分推荐...")
        recommendations = find_recommendations(
            args.session_token,
            "../difficulty.tsv",
            save_data,
            target_increase=args.target,
            top_n=args.top
        )
        
        print(json.dumps(recommendations, ensure_ascii=False))
    
    elif args.command == "update-difficulty":
        import shutil
        
        difficulty_path = "../difficulty.tsv"
        
        if os.path.exists(difficulty_path):
            logger.info(f"删除旧的难度定数表: {difficulty_path}")
            os.remove(difficulty_path)
        
        metadata_dir = os.path.join(OUTPUT_DIR, "metadata")
        if os.path.exists(metadata_dir):
            logger.info(f"删除旧的元数据目录: {metadata_dir}")
            shutil.rmtree(metadata_dir)
        
        apk_path = get_apk_path(args.apk_path, logger)
        if not apk_path:
            logger.error("无法获取APK文件，退出")
            sys.exit(1)
        
        logger.info(f"使用APK: {apk_path}")
        
        from gameInformation import run as game_info_run
        from log import init_console_logger
        from configparser import ConfigParser
        
        c = ConfigParser()
        c.read("config.ini", "utf8")
        types = c["TYPES"]
        config = {
            "avatar": types.getboolean("avatar"),
            "chart": types.getboolean("Chart"),
            "illustrationBlur": types.getboolean("IllustrationBlur"),
            "illustrationLowRes": types.getboolean("IllustrationLowRes"),
            "illustration": types.getboolean("Illustration"),
            "music": types.getboolean("music")
        }
        
        ensure_output_dirs(config)
        internal_logger = init_console_logger()
        
        logger.info("提取元数据...")
        game_info_run(apk_path, internal_logger, METADATA_DIR)
        
        extracted_difficulty = os.path.join(METADATA_DIR, "difficulty.tsv")
        if os.path.exists(extracted_difficulty):
            logger.info(f"复制难度定数表到项目根目录...")
            shutil.copy(extracted_difficulty, difficulty_path)
            logger.success(f"难度定数表更新完成！")
            print(json.dumps({
                "status": "success",
                "source": extracted_difficulty,
                "destination": difficulty_path
            }))
        else:
            logger.error("未能提取难度定数表")
            sys.exit(1)
    
    else:
        if not args.silent:
            parser.print_help()

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()