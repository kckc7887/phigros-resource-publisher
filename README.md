# Phigros 全量发布（GitHub Actions 版）

只有一个手动触发的工作流：填好密钥 → 手动 Run → 自动完成 **下载最新 APK → 全量解包（含全曲音乐）→ 全量上传**。音乐等大文件一律提取、一律上传，不做任何裁剪。

## 需要配置的 Secrets

在仓库 **Settings → Secrets and variables → Actions → Secrets** 中配置：

| Secret | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `S3_BUCKET` | 是 | — | 对象存储桶名 |
| `S3_ACCESS_KEY` | 是 | — | Access Key |
| `S3_SECRET_KEY` | 是 | — | Secret Key |
| `S3_ENDPOINT` | 否 | `https://cn-nb1.rains3.com` | S3 兼容端点（雨云等） |
| `S3_PUBLIC_BASE` | 否 | 空 | 公网访问基址，仅用于汇总中的 `current.json` 直链 |

密钥只经 Secrets 注入环境变量，不写入日志与文件。

## 使用方法

1. 把本仓库推到 GitHub（或直接在本仓库操作）。
2. 按上表配置 Secrets。
3. 进入 **Actions** → **Phigros 全量发布** → **Run workflow** → 手动运行。
4. 运行结束后：
   - 日志中可看到下载进度、解包（含音乐提取）、逐批上传进度；
   - Summary 页有发布结果表（版本、资产数、总大小、上传/清理对象数、耗时）；
   - Artifact `phigros-release-manifests` 归档了 `current.json` / `manifest.json` / `catalog.json` / `note_counts.tsv` / `summary.json`。

## 工作流做了什么

1. **下载**：通过 TapTap 接口查询最新 Phigros 版本，校验下载地址（HTTP 200）后流式下载 APK。
2. **全量解包**：内置 phiTool 工具链解出头像、全谱面、曲绘（原图 / 模糊 / 低清）、**全曲 `.ogg` 音乐**、元数据，并统计全曲物量表；音乐重建依赖系统 `libogg` / `libvorbis`（工作流自动 apt 安装）。
3. **整理**：生成发布目录与 `manifest.json`（逐文件 SHA-256）、`catalog.json`、`note_counts.tsv`、`current.json`。
4. **全量上传**：把 `phigros/releases/<版本>/` 全部资产上传到对象存储，最后上传 `phigros/current.json`（no-cache）；随后清空桶内 `phigros/releases/` 下所有旧对象，仅保留本次上传。

## 发布产物结构

```text
<桶>/
└── phigros/
    ├── current.json
    └── releases/<游戏版本>/
        ├── manifest.json
        ├── catalog.json
        ├── avatars/
        ├── charts/<歌曲ID>/{EZ,HD,IN,AT}.json
        ├── illustrations/
        ├── illustrations-blur/
        ├── illustrations-lowres/
        ├── music/<歌曲ID>.ogg
        └── metadata/{difficulty,info,note_counts}.tsv
```

## 本地运行

```bash
python -m pip install -r requirements.txt
# Linux 需先安装音频库：sudo apt-get install -y libogg0 libvorbis0a
export S3_BUCKET=... S3_ACCESS_KEY=... S3_SECRET_KEY=...
python publish.py
```

本地产物写入 `work/`（已 gitignore），每次运行只保留最新一份。

## 已知边界

- TapTap 下载接口不承诺长期稳定；接口变动时下载阶段会失败并红脸退出。
- 音乐重建在 Linux 依赖 `libogg` / `libvorbis`（fsb5 官方支持路径，工作流已自动安装）；`bundled/` 内的 Windows DLL 仅用于本地 Windows 复用。
- 上传走 S3 兼容接口（boto3，已适配雨云的 path 寻址与 DeleteObjects Content-MD5 要求）。
- 全量上传会删除桶内 `phigros/releases/` 下所有旧对象，仅保留本次版本。

## 致谢与许可

- 解包工具链 [phiTool](https://github.com/Chnynnya/phiTool)（GPL-3.0，见 `bundled/phiTool/script-py/` 文件头）。
- 音乐重建依赖 [python-fsb5](https://github.com/HearthSim/python-fsb5) 与 Xiph.Org 的 libogg / libvorbis（BSD-3，见 `bundled/phiTool/script-py/LICENSE-xiph.txt`）。
- 资源解析依赖 [UnityPy](https://github.com/K0lb3/UnityPy)。
