# Phigros 资源发布

每天北京时间 **08:00**（GitHub cron `0 0 * * *`，UTC）和手动运行时检查资源。GitHub 的定时任务可能排队延迟；push / pull request 只运行依赖预检和测试。Phigros 与 Rizline 位于不同仓库，可以同时运行；本仓库发布使用固定并发组，不取消正在上传的任务。

发布流程为 **下载最新 APK → 并行完整解析 → 比较 SHA-256 资源清单 → 有变化时完整发布 → 校验并切换 → 精准清理旧发布**。

## 配置

在 GitHub Actions Secrets 中配置：

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `S3_BUCKET` | 是 | 桶名 |
| `S3_ACCESS_KEY` | 是 | Access Key |
| `S3_SECRET_KEY` | 是 | Secret Key |
| `S3_ENDPOINT` | 否 | 默认 `https://cn-nb1.rains3.com` |
| `S3_PUBLIC_BASE` | 否 | 公网基址，仅用于汇总链接 |

可选 Actions Variables：

| 名称 | 默认 | 范围 | 作用 |
| --- | --- | --- | --- |
| `PHIGROS_PARSE_WORKERS` | 4 | 1–16 | 资源解包、物量统计、文件整理、SHA-256 和音乐校验的有界并发 |
| `S3_UPLOAD_WORKERS` | 8 | 1–32 | 上传及远端回读校验的有界并发 |

凭据只经环境变量提供。需要 GetObject、PutObject、ListBucket 和 DeleteObject 权限；发布指针还要求端点正确支持 `If-Match` / `If-None-Match` 条件写入。SDK 缺少条件参数时在上传资源前退出；端点拒绝条件写入时发布失败，绝不回退到无条件覆盖。

每次需要发布时，先在 `phigros/publisher-checks/<随机键>` 用少量测试字节验证条件写入：错误 ETag 和对已有键的首次写入条件必须拒绝，正确 ETag 必须成功；每次回读检查内容，最后只清理该测试键。端点静默忽略条件头同样会被拒绝。资源完全相同时跳过测试写入。

## 资源清单与发布行为

复用已有 `manifest.json`，不增加第二套资源清单。每个资源条目包含相对路径、大小、SHA-256、Content-Type；`current.json` 记录清单自身的 SHA-256。

先读取 S3 `phigros/current.json` 及其清单。比较排序后的资源路径、大小、SHA-256、Content-Type 和游戏版本；生成时间、随机修订号不参与资源等同性比较。完全一致时只读取这两份 JSON，跳过资源上传、资源回读、指针更新及清理。清单缺失、损坏、无法验证，或任一资源变化时，**完整上传全部本地资源**。403、网络失败等访问错误会使任务失败，不当作“清单不存在”。

每轮解析都生成全新的 `phigros/releases/<游戏版本>-<独立修订号>/`。即使游戏版本相同，也不会覆盖正在使用的资源目录；候选前缀已存在时拒绝使用。完整解析保留头像、全谱面、曲绘原图/模糊/低清、所有音乐和元数据。

所有资源并行上传后逐个从 S3 流式回读，核对实际字节数和 SHA-256；仅凭 ETag 或自填 metadata 不算校验通过。全部成功后上传并验证 manifest，最后使用开始时捕获的 ETag 条件更新 current（首次发布使用 `If-None-Match: *`），随后回读确认。资源或 manifest 阶段失败时不改指针；条件冲突不会覆盖其它发布。current 写入或回读失败时停止清理，指针可能已经更新，报告会记录 `commit_attempted` 和 `pointer_verified`，不会自动回滚；候选目录保留用于诊断。

切换确认后，立即删除**本轮开始时捕获的旧 current 所指目录中的对象快照**，每批删除前和结束后再次检查 current。不会扫描并删除全部 `releases/`，不会删除其它游戏、其它候选、历史无关目录或上传后新出现的对象。删除响应中的逐对象失败会保存在报告中，任务失败；不回滚已确认成功的新指针。

上传只使用一层并发，关闭 boto3 分片传输内层线程，排队任务不超过并发数的两倍；连接超时 15 秒、读超时 120 秒、每次 SDK 操作最多 5 次尝试。并行任务会传播错误；无未验证的半成品发布到 current。

上传期间旧目录和 current 完整保留。切换后立即清理意味着仍持有旧 URL 的客户端必须通过 current 刷新并恢复；离线下载到本地的文件不受影响。客户端必须按 current 中的资源目录加载资源，不能自行用 gameVersion 拼接发布路径。对象存储兼容性、真实吞吐和旧 URL 的客户端恢复仍需上线验收，自动测试不代表实际雨云端点已验证。

## 产物与失败恢复

Actions 的 `phigros-release-manifests` artifact（始终归档，保留 30 天）包含 `current.json`、`manifest.json`、`catalog.json`、`note_counts.tsv`、`summary.json`、`publication.json` 和 `run-status.json`。没有变化时 current 和 manifest 保存相互匹配的线上内容，`candidate-current.json` / `candidate-manifest.json` 保存未发布的候选；catalog 和物量表来自内容相同的本地解析。每次运行保存在 `work/artifacts/<UTC时间-运行号>/`，后续运行保留已有失败报告。上传失败仍保留报告和可生成的汇总。

`publication.json` 记录候选前缀、旧对象快照、提交与回读状态、上传/验证数、已删除数和 `cleanup_remaining`。只有已确认指针切换成功的报告可以自动重试清理。在下载该 artifact 并配置相同 S3 环境变量后运行：

```sh
python -m phigros_publisher.uploader --retry-cleanup /path/to/publication.json
```

重试只删除原始快照中尚未确认成功的键，不重新上传资源、不改指针、不扩大扫描范围。报告与桶/端点不匹配、current 已变化或报告路径越界时拒绝清理。不要手动扩大报告的对象列表。

## 本地运行与验证

```sh
python -m pip install -r requirements.txt
# Linux 音频依赖：sudo apt-get install -y libogg0 libvorbis0a libvorbisenc2 ffmpeg
# 配置 S3_BUCKET、S3_ACCESS_KEY、S3_SECRET_KEY 等环境变量后：
python publish.py
python -m unittest discover -s tests -v
python -c "from phigros_publisher.extract_cli import preflight_audio; preflight_audio()"
```

本地产物保存在已忽略的 `work/`，不提交资源、APK 或凭据。Windows 音频预检需要在 `bundled/phiTool/script-py` 作为当前目录，以加载随工具链提供的 DLL；FFmpeg / ffprobe 需要在 PATH 中。

发布前校验本地文件集合、大小、SHA-256、歌曲音乐覆盖、谱面难度和发布指针。音乐经 ffprobe 检查 Vorbis 音轨、采样率、声道及正时长；默认 `.0/music.wav` 导出为 `<songId>.ogg`，其它编号变体有各自音乐，不能掩盖默认谱面缺失。测试覆盖清单无变化/缺失/损坏、全量更新、条件冲突、远端损坏、并发上限、准确清理与失败重试，不连接真实 S3。

## 致谢与许可

解包工具链 [phiTool](https://github.com/Chnynnya/phiTool) 使用 GPL-3.0；音乐重建依赖 [python-fsb5](https://github.com/HearthSim/python-fsb5) 和 Xiph.Org libogg / libvorbis / libvorbisenc；Unity 资源解析依赖 [UnityPy](https://github.com/K0lb3/UnityPy)。许可文件保留在 `bundled/phiTool/script-py/`。
