# Immich Camera Importer

把 DJI Pocket 3 / Insta360 ONE RS 素材只读导入 **Immich 3.1.0**，验证相机 metadata，并独立保存有价值的 WAV companion。运行于普通 Linux / Synology DSM 用户环境，无需 Docker、sudo 或数据库访问。

```sh
camera-import --dry-run /volumeUSB1/usbshare/DCIM
camera-import /volumeUSB1/usbshare/DCIM
camera-import .
camera-import --verbose /some/history-folder
camera-import --json /some/history-folder
```

## DSM：无需 pip 的单文件脚本

需要 Python **3.8+**、Perl / **ExifTool**，以及已经登录的 Immich CLI。兼容 DSM 7.2.2 自带的 Python 3.8.15，没有任何第三方 Python 依赖。

只需下载仓库根目录的 [`camera-import.py`](camera-import.py)，放在素材目录之外：

```sh
python3 /path/to/camera-import.py --dry-run /volumeUSB1/usbshare/DCIM
python3 /path/to/camera-import.py /volumeUSB1/usbshare/DCIM
python3 /path/to/camera-import.py --verbose /some/history-folder
```

脚本包含全部 importer 模块，直接在内存中加载；无需解压、安装包或把 `src` 目录一起复制。其他选项和配置与 `camera-import` 命令相同。

若希望保留简短命令，可在 shell 配置中定义：

```sh
camera-import() { python3 /path/to/camera-import.py "$@"; }
```

有 pip 的环境仍可选择 `python3 -m pip install --user .` 安装命令入口，但 DSM 不需要此步骤。

ExifTool 只用于**读取**媒体 metadata。可以使用已安装的 `exiftool`，或者把 [ExifTool 官方 Perl distribution](https://exiftool.org/install.html#Unix) 解压到用户可读目录，并配置可执行文件的绝对路径：

```sh
export EXIFTOOL_BIN="$HOME/.local/opt/Image-ExifTool/exiftool"
immich server-info
```

无需运行 ExifTool 的系统安装步骤；保留解压包内的 `lib` 目录。`--dry-run` 也会读取 metadata，因此媒体分类需要 ExifTool。只含 WAV / 已知 proxy 的目录不需要 ExifTool。

## 路由

| 素材 | 目的地 | Camera / Lens |
|---|---|---|
| Pocket 3 `DJI_...JPG/JPEG/MP4` | Immich timeline | DJI / DJI OsmoPocket3；保留原镜头字段 |
| Pocket 3 `DJI_...WAV` | Companion archive，SHA-256 验证 | filename 日期 |
| Pocket 3 `DJI_...LRF` | 明确忽略，源文件保留 | — |
| ONE RS `VID_..._00_....mp4` | Immich timeline | Insta360 / Insta360 OneRS / 4K Boost Lens |
| ONE RS `LRV_..._01_....mp4` | 明确忽略，源文件保留 | — |
| ONE RS `VID_..._00_....insv` | Immich archive | Insta360 / Insta360 OneRS / 5.7K 360 Lens |
| ONE RS `VID_..._10_....insv` | Immich archive | 同上 |
| ONE RS `LRV_..._11_....insv` | Immich timeline | 同上 |
| 明确可独立保存的 INSP / JPEG / DNG | Immich timeline | 必须由 metadata 确认机型；不猜镜头 |
| `Thumb/` 内 JPG/JPEG/PNG/BMP/THM | 明确忽略，源文件保留 | 不把任意 Thumb 子文件都当缓存 |
| 其他文件、symlink、特殊文件 | UNKNOWN / NEEDS REVIEW | 最终失败，保留源文件 |

文件名不匹配已知规则、未来的新 channel、空文件、无效日期、疑似多文件照片都会要求检查。通用 `DJI_...` 命名以本项目的 Pocket 3 输入范围解释；若 metadata 明确显示其他 DJI 机型，则报冲突。照片中的其他相机型号不会被改成 ONE RS。

支持 INSV 入库不代表支持拼接，也不保证 raw 双鱼眼视频能作为正常全景播放；master 的原字节仍由 Immich 管理。

## Metadata 和 XMP

1. 读取媒体的 Make / Model / Lens 字段。
2. 已有正确的 embedded metadata 不额外生成 XMP；缺失或需要统一时生成标准 XMP。
3. `tiff:Make` / `tiff:Model` 保存指定字符串；镜头用 **`exifEX:LensModel`**。
4. 小型 XMP 在内存中生成，作为 multipart `sidecarData` 和原媒体 `assetData` **同一请求上传**。XMP part 文件名是 `original.ext.xmp`。媒体由文件句柄流式发送，不复制到 staging，也不整体载入内存。
5. API 读取 asset，验证 owner、SHA-1、managed-library 身份、visibility 和相机字段；请求 `refresh-metadata`，限时轮询到期望值。

源目录中的已有关联 XMP 会安全解析并合并，只更改要求标准化的相机属性，其他属性保留。同时存在两种候选命名、多个媒体共用一个 sidecar、无效 XML 或过大的 XMP 会报错。孤立 XMP 仍是 UNKNOWN。

**Lens 的特别处理：** Immich 3.1.0 优先读取 `LensID → LensType → LensSpec → LensModel`。ExifTool 会把填入文本的 `aux:LensID` 转成 `Unknown (...)`，因此本工具不会这样写。如果源文件已有会遮盖目标 LensModel 的冲突字段，则报告 NEEDS REVIEW；不伪造数字 ID。服务端验证不符合目标值时始终失败。

**已有 asset 不补传 XMP。** 重复导入会读取并验证已有 metadata，修复 visibility，必要的 extraction refresh 可以重跑。已有 metadata 不正确、XMP 缺失或不生效时，返回非零；不删除重建 asset、不直接操作数据库或 managed storage。

`GET /assets/{id}` 不提供独立的 sidecar 内容回读接口。本工具的新上传依赖 v3.1.0 官方 multipart sidecar 持久化路径，并验证最终 metadata。API 任务异步执行，轮询正确字段不等于提供独立的任务完成收据；工具不会声称下载核验过服务器端 XMP。完整接口依据见 [实现说明](docs/immich-3.1.0.md)。

## 配置

优先级：**命令行 > 环境变量 > config JSON > 默认值**。

默认读取 `~/.config/camera-import/config.json`；可用 `--config PATH` 覆盖。

```json
{
  "companion_root": "/volume1/immich/companions",
  "manifest_root": "/volume1/immich/manifests",
  "immich_bin": "immich",
  "exiftool_bin": "exiftool",
  "expected_user_name": "Camera Archive",
  "verify_timeout": 180,
  "poll_interval": 2,
  "request_timeout": 300
}
```

| 环境变量 | 用途 |
|---|---|
| `COMPANION_ROOT` | WAV archive 根目录 |
| `MANIFEST_ROOT` | Bundle manifest / run report 根目录 |
| `IMMICH_BIN` | Immich CLI 可执行文件 |
| `EXIFTOOL_BIN` | ExifTool 可执行文件 |
| `IMMICH_CONFIG_DIR` | Immich CLI `auth.yml` 所在目录 |
| `IMMICH_API_URL` | API 地址，如 `http://127.0.0.1:2283/api` |
| `IMMICH_API_KEY` | 明确提供 API key；必须同时给 API URL |
| `IMMICH_EXPECTED_USER_ID` | 可选：用稳定 user ID 校验身份，优先于显示名称 |
| `IMMICH_EXPECTED_USER_NAME` | 默认 `Camera Archive` |

默认复用 `~/.config/immich/auth.yml` 中的 `url` / `key`，兼容旧 `instanceUrl` / `apiKey`。覆盖 API URL 时，若地址与 CLI 登录地址不一致，必须显式给对应 key，避免把旧 key 发给另一台服务器。API key 不接受写进项目 JSON、不放在命令行或日志中。

账户需具备相应的 API 权限：`user.read`、`server.about`、`asset.statistics`（CLI server-info）、`asset.upload`、`asset.read`、`asset.update`、`job.create`（metadata refresh）。以服务器的权限设置为准；权限不足明确失败。Storage Label `camera` 和 Storage Template 由 Immich 自己设置和维护。

| 选项 | 行为 |
|---|---|
| `[PATH]` | 默认 `.` |
| `--dry-run` | 只读本地计划；不连接 API、不创建输出目录、不归档、不写 manifest |
| `--verbose` | stderr 输出分类、hash、无 secret 的 API 状态 |
| `--json` | stdout 只有一个 JSON report，适合自动化 |
| `--strict` | 计划发现 unknown、incomplete 或其他错误时，执行写操作前停止 |
| `--no-metadata-verify` | 调试用途；始终返回非成功安全结论 |
| `--companion-root PATH` | 覆盖 WAV 根目录 |
| `--manifest-root PATH` | 覆盖 manifest 根目录 |
| `--api-url URL` | 覆盖 API 地址 |
| `--auth-dir PATH` | 覆盖 CLI auth 目录 |
| `--verify-timeout SECONDS` | 每个 asset 的 metadata 等待上限 |

## 数据和重复运行

```text
/volume1/immich/companions/pocket3/YYYY/YYYY-MM-DD/original.WAV
/volume1/immich/manifests/insta360/YYYYMMDD_HHMMSS_sequence.json
/volume1/immich/manifests/runs/<UTC-time>-<unique-id>.json
```

- WAV 先写目标目录内临时文件，fsync 后核验 SHA-256，再用不覆盖已有文件的原子操作发布。已有同 hash 文件跳过，不同 hash 永不覆盖。日期取 filename，不取 mtime。
- 媒体同时计算 SHA-1（Immich checksum）和 SHA-256（manifest）。相同 checksum 对应冲突角色或相机信息时，不尝试反复修改同一个 asset。
- Manifest 记录 bundle key、role、原 filename、size、SHA-256、Immich checksum、asset ID、owner 和服务器身份。锁定后检查旧记录，合并缺失信息，再原子写入；冲突保留原文件并失败。
- 允许保存不完整 bundle 的部分进度，但不会把它算作安全完成。重复运行能补齐缺失成员的 asset ID。
- 网络中断后直接重复执行同一命令即可通过 checksum 找到已有资源；不依赖上次报告决定跳过验证。
- 默认已知素材继续处理，但 unknown、不完整 bundle 或任何错误仍导致最终非零退出。
- 正常运行结束重新检查源文件状态及目录清单。发现新文件、文件变化或扫描失败，不能给出成功结论。
- 输出目录不能和 source 重叠，拒绝 symlink 输出目录，禁止配置到 `/volume1/immich/media` 内。其他部署的 managed storage 也不能用作输出根目录。

不触碰 `/volume1/immich/media/library`，没有删除、重命名、修改源媒体或格式化命令。正常读取可能由文件系统更新 access time；工具不写源文件内容或 metadata。

只有全部必要验证成功时输出：

```text
RESULT: SAFE TO REVIEW FOR CARD FORMAT
```

失败输出 `NOT SAFE TO FORMAT SOURCE`；退出码 1，用户中断为 130。dry-run 全部计划检查通过输出 `DRY RUN OK`，这不代表媒体已备份。只有 ignored 文件的目录也不能作为已归档成功的证明。

## 测试

```sh
PYTHONPATH=src python3 -S -m unittest discover -s tests -v
```

测试包含真实本地 HTTP multipart、checksum 去重、metadata 失败、visibility 修复、上传响应丢失后的恢复、WAV 冲突、manifest 冲突、源文件变化和 dry-run 零写入。设置 `EXIFTOOL_BIN` 或安装 ExifTool 可启用真实 XMP 解析测试。测试不会连接你的 Immich 或 NAS。

当前验证范围是 **v3.1.0 源码契约 + 本地模拟服务 + 真实 ExifTool**，不是对真实 Pocket 3 / ONE RS 素材和 DSM 的端到端验收。服务端版本不匹配会明确停止。

单文件脚本由 `python3 tools/build_standalone.py` 从 `src` 生成。CI 会检查生成结果与源码一致，并在 `python -S`（不加载 site-packages）的环境下运行全部测试。

CLI 的 `auth.yml` 使用无依赖的严格标量解析器，支持普通值、单/双引号和注释；复杂 YAML（锚点、标签、多行值）明确拒绝，可改用环境变量凭据。XMP 使用标准库 XML 解析，拒绝 DTD、实体声明和非 UTF-8 输入，避免隐式外部资源读取。
