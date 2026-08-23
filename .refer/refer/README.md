# Phigros 资源发布 GUI Demo

本地概念验证：选择本地 APK 或下载最新客户端，解包、整理静态发布目录，并按需上传雨云对象存储。提供 tkinter 原生 GUI，可打包为零依赖单文件 exe。

## APK 来源

- **本地 APK**：选择或填写本机已有 APK 路径。版本号默认从 `Phigros_<版本>.apk` 文件名推断。
- **下载最新 APK**：从 TapTap 下载最新 APK，写入固定目录 `work/cache/latest-apk/`（每次下载前清空，只保留最后一次）。

## 执行动作

- **仅解析整理**：解包并整理到 `work/latest/`，不连接对象存储。
- **解析并上传**：解包整理后，按「上传范围」上传。
- **仅上传已有结果**：不重新解包，直接上传 `work/latest` 中已有产物的指定内容。

## 上传范围

| 范围 | 内容 |
| --- | --- |
| `all` | 全部资源；上传后清空桶内 `phigros/releases/`，仅保留本次上传对象 |
| `current` | 仅 `current.json` |
| `catalog` | 仅 `catalog.json` |
| `manifest` | 仅 `manifest.json` |
| `note_counts` | 仅物量表 `metadata/note_counts.tsv` |
| `metadata` | 整个 `metadata/` |
| `charts` | 整个 `charts/` |
| `avatars` | 整个 `avatars/` |
| `illustrations` | 原图 / 模糊 / 低清曲绘 |
| `music` | 整个 `music/`（全曲 `.ogg`） |

局部上传不会删除桶内其他对象。

解包工具链内置在 `bundled/phiTool/`。本地解包与发布产物始终写入 `work/latest/`，每次解析前清空，只保留最新一份。

勾选「提取音乐」时，解包阶段会从 APK 内 FSB5 音频重建全曲 `.ogg` 到 `music/`，随后随 `all` 或 `music` 范围参与上传；不勾选则不提取、不占用该部分磁盘空间（数百 MiB 至数 GiB）。音乐重建依赖内置的 Xiph `libogg` / `libvorbis` DLL（随工具链打包，BSD-3 许可，详见 `bundled/phiTool/script-py/LICENSE-xiph.txt`）。

勾选「记住密钥」时，Access Key / Secret Key 与上传配置写入可写根目录下的 `config.json`（源码模式为 Demo 根目录；exe 模式为 exe 同目录）。取消勾选后再保存会清空已存密钥。密钥不会写入日志。

## 源码启动

```powershell
cd D:\Projects\rRanker\demo\phigros-resource-publisher
python -m pip install -r requirements.txt
python gui.py
```

## 打包单文件 exe

```powershell
cd D:\Projects\rRanker\demo\phigros-resource-publisher
python -m pip install -r requirements.txt pyinstaller
python build.py
```

产出：`dist/PhigrosResourcePublisher.exe`。终端用户无需安装 Python；首次启动 onefile 解压可能稍慢。工作目录与 `config.json` 写在 exe 同目录。

旧的 `app.py` + `web/` 本地 WebUI 入口已弃用，请改用 `gui.py`。

## 验证

快速单元测试：

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

显式执行最新下载地址 HTTP 200 探测：

```powershell
$env:PHIGROS_LIVE_TEST="1"
python -m unittest tests.test_live_probe
```

执行本地 APK 到上传前的完整验证：

```powershell
python validate_local.py --apk D:\path\to\Phigros_3.19.4.apk
```

完整解包会生成数百 MiB 至约 1 GiB 资源，需要足够磁盘空间和数分钟处理时间。

## 发布产物

每次运行写入固定目录 `work/latest/release/`（运行前清空旧内容）：

```text
work/
├── cache/latest-apk/          # 仅下载模式；只保留最后一次 APK
└── latest/
    ├── toolchain/             # 解包中间产物
    └── release/
        └── phigros/
            ├── current.json
            └── releases/<游戏版本>/
                ├── manifest.json
                ├── catalog.json
                ├── avatars/
                ├── charts/
                │   └── <歌曲ID>/
                │       ├── EZ.json
                │       ├── HD.json
                │       ├── IN.json
                │       └── AT.json
                ├── illustrations/
                ├── illustrations-blur/
                ├── illustrations-lowres/
                ├── music/                # 仅勾选「提取音乐」时存在
                │   └── <歌曲ID>.ogg
                └── metadata/
                    ├── difficulty.tsv
                    ├── info.tsv
                    └── note_counts.tsv
```

`metadata/note_counts.tsv` 为全曲物量表：每行 `歌曲ID` + 3 或 4 列难度物量，每列为 JSON 数组 `[Tap,Hold,Drag,Flick]`（官方 type 1/3/2/4）。无 AT 难度的歌曲仅含 EZ/HD/IN 三列。

上传时按所选范围上传；全量上传时 `phigros/current.json` 最后上传，然后删除 `phigros/releases/` 下所有不在本轮上传集合中的对象（含其他版本与同版本孤儿文件）。局部上传只覆盖所选对象。

## 已知边界

- Demo 内置 phiTool 解包脚本，TapTap 下载接口不承诺长期稳定。
- 音乐提取依赖 `fsb5`（纯 Python）与内置 `libogg` / `libvorbis` DLL；DLL 经 fsb5 的工作目录回退机制加载，exe 模式下已随工具链一并打包。
- 没有上传密钥时不会连接对象存储；自动化上传依赖 S3 兼容接口和 `boto3`。
- 这是本地 POC，不是移动客户端运行时依赖，也不保存任何玩家数据。
- onefile exe 体积较大（含 UnityPy / phiTool），属预期。
- 解包在 GUI 进程内执行（不再 subprocess 调自身 exe），避免 frozen 下 WinError 267。
