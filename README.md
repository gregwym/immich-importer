# Immich Camera Importer

把 DJI Pocket 3 / Insta360 ONE RS 素材只读导入 **Immich 3.1.0**，验证相机 metadata，并独立保存有价值的 WAV companion。运行于普通 Linux / Synology DSM 用户环境，无需 Docker、sudo 或数据库访问。

```sh
camera-import --dry-run /volumeUSB1/usbshare/DCIM
camera-import /volumeUSB1/usbshare/DCIM
camera-import .
camera-import --verbose /some/history-folder
camera-import --json /some/history-folder
```

## DSM 一键安装（clone 后）

```sh
sh install.sh
```

安装器自动安装 **camera-import、ExifTool 和 Immich CLI**，不需要 pip、sudo 或 root。已存在且可运行的工具会复用；不重新登录，不改动 API key、素材、companions 或 manifests。

| 类别 | 安装器行为 |
|---|---|
| Python ≥ 3.8（包括 DSM 3.8.15） | 只检查，不安装运行时 |
| Node ≥ 20、Perl | 只检查，不安装运行时 |
| npm | 安装 CLI 时必须可用；不自动安装 npm 本身 |
| Immich CLI | 缺失时执行 `npm install --global --prefix "$HOME/.local" --engine-strict --no-audit --no-fund @immich/cli@3.1.0` |
| ExifTool | 缺失时下载官方 GitHub 仓库固定 commit 的 13.59 源码包，校验固定 SHA-256 后解压，无需系统安装 |
| camera-import | 安装单文件脚本并生成 shell wrapper，记录 Python、ExifTool、CLI 路径 |

安装后命令：

```sh
camera-import --dry-run /some/folder
camera-import /some/folder
```

默认位置：

```text
~/.local/bin/camera-import
~/.local/bin/exiftool                 # 仅需安装 ExifTool 时创建
~/.local/bin/immich                   # npm 全局安装位置
~/.local/lib/node_modules/@immich/cli/ # npm 管理
~/.local/share/immich-importer/
```

安装器幂等地向 `~/.profile` 添加 PATH。当前父 shell 不会被子进程改变，新登录会话即可使用短命令；当前会话可用 `~/.local/bin/camera-import` 或执行 `. ~/.profile`。

再次执行 `sh install.sh` 即更新 importer，并复用现有依赖。安装过程只运行 `--version` / `-ver` 自检，不连接 Immich、不上传文件，也不触发登录。如果原本未登录，正常导入时仍需要已有的 `immich login` 凭据。

可选参数和环境变量：

```sh
sh install.sh --no-profile
sh install.sh --prefix /your/user/local
PYTHON_BIN=/path/to/python3 NODE_BIN=/path/to/node PERL_BIN=/path/to/perl sh install.sh
```

`--prefix` 同时控制工具目录和 npm prefix，默认 `~/.local`；`NPM_BIN` 可指定 npm。已安装工具可通过 `IMMICH_BIN` / `EXIFTOOL_BIN` 指定。离线 ExifTool 包可用 `--exiftool-archive PATH`，仍要求同一固定 SHA-256；缺少 CLI 的安装仍需 npm 网络或缓存。

安装器不会覆盖不属于它的 `camera-import` / `exiftool` 命令；npm 安装失败时返回非零。下载安装的 ExifTool 固定源及 checksum 记录在 [`tools/install.py`](tools/install.py)。

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

扫描时 stderr 会持续输出进度（`[n/total] metadata: 文件`、hashing、upload/verify），`--quiet` 可关闭。metadata 读取按每批 50 个文件调用一次 ExifTool，避免在 NAS 上逐个文件启动 Perl；单个文件读取失败时再单独重试并精确报告。

ExifTool 只用于**读取**媒体 metadata。可以使用已安装的 `exiftool`，或者把 [ExifTool 官方 Perl distribution](https://exiftool.org/install.html#Unix) 解压到用户可读目录，并配置可执行文件的绝对路径：

```sh
export EXIFTOOL_BIN="$HOME/.local/opt/Image-ExifTool/exiftool"
immich server-info
```

无需运行 ExifTool 的系统安装步骤；保留解压包内的 `lib` 目录。`--dry-run` 也会读取 metadata，因此媒体分类需要 ExifTool。只含 WAV / 已知 proxy 的目录不需要 ExifTool。

## 路由

| 素材 | 目的地 | Camera / Lens |
|---|---|---|
| Pocket 3 `DJI_...MP4` | Immich timeline | DJI / DJI OsmoPocket3；保留原镜头字段 |
| Pocket 3 `DJI_...JPG/JPEG/DNG` | Immich timeline；同名 JPG + DNG 组成一个 Immich stack，JPG 为主 | 保留照片自带 EXIF；无 EXIF 时写 DJI / DJI OsmoPocket3 |
| Pocket 3 `DJI_...WAV/AAC` | Companion archive，SHA-256 验证 | filename 日期 |
| Pocket 3 `DJI_...LRF` | 明确忽略，源文件保留 | — |
| ONE RS `VID_..._00/10_....mp4`、`PRO_VID_..._00/10_....mp4`（HDR/PRO） | Immich timeline | Insta360 / Insta360 OneRS / 4K Boost Lens |
| ONE RS `LRV_..._01/11_....mp4`、`PRO_LRV_...mp4` | 明确忽略，源文件保留 | — |
| ONE RS `VID_..._00_....insv`、`PRO_VID_..._00_....insv` | Immich archive；bundle 缺少 LRV 时改为 timeline | Insta360 / Insta360 OneRS / 5.7K 360 Lens |
| ONE RS `VID_..._10_....insv`、`PRO_VID_..._10_....insv` | Immich archive | 同上 |
| ONE RS `LRV_..._11_....insv`、`PRO_LRV_..._11_....insv` | Immich timeline | 同上 |
| 明确可独立保存的 INSP / JPEG / DNG（含 HDR 包围曝光的多张 `IMG_` 原片） | Immich timeline；同名 JPG/INSP + DNG 组成一个 stack | 必须由 metadata 确认机型；保留照片自带 EXIF |
| `Thumb/` 内 JPG/JPEG/PNG/BMP/THM | 明确忽略，源文件保留 | 不把任意 Thumb 子文件都当缓存 |
| `@` 或 `.` 开头的目录（DSM `@eaDir`、`@Recycle`、`.hidden`） | 整个目录跳过，不进入、不分类 | 报告中列为 skipped |
| 其他文件、symlink、特殊文件 | UNKNOWN / NEEDS REVIEW | 最终失败，保留源文件 |

文件名不匹配已知规则、未来的新 channel、空文件、无效日期、疑似多文件照片都会要求检查。

**文件命名参考。** Insta360 ONE RS：`[PRO_]{VID|LRV|IMG}_YYYYMMDD_HHMMSS_{00,01,10,11}_XXX.{mp4|insv|insp|jpg|dng}`。两位数第一位是镜头（0 / 1），第二位 0 是 master、1 是 LRV 代理；`PRO_` 前缀表示 HDR / PRO 类模式（Active HDR、FreeFrame 等），Insta360 官方说明这类文件需要在 App / Studio 中后处理，本工具按同样的 channel 规则入库。4K Boost 镜头的 master 可能出现在 `00` 或 `10` channel，两者都写入 `4K Boost Lens`。HDR 照片在卡上是 3 张同一时间戳、序号连续的 `IMG_` 原片，逐张作为独立照片进入 timeline；合成需要 Insta360 App。DJI Pocket 3：`DJI_YYYYMMDDHHMMSS_NNNN_D.{MP4|JPG|DNG|WAV|AAC|LRF}`，AAC 是开启麦克风备份时与 WAV 同步生成的音频文件，与 WAV 同样归档。未在本表内的前缀（例如 X 系列的 `TIM_`、`INTV_`）仍报 UNKNOWN，确认后再补充。

**360 bundle 缺少 LRV：** 一个 bundle 由 `VID_..._00`、`VID_..._10` 两个 master 和 `LRV_..._11` 代理组成。LRV 可由 master 重新生成，删除它不丢失任何原始数据，因此只缺 LRV 的 bundle 不算 incomplete：`VID_..._00_....insv` 改为进入 timeline，作为这段视频的可见条目，`VID_..._10` 仍进入 archive。报告中以 `lrvMissing` 列出这些 bundle。缺少任一 master 仍是 INCOMPLETE 360 BUNDLE 并导致非零退出。之前按 archive 导入的 master，在 LRV 删除后重新运行会把已有 asset 的 visibility 修正为 timeline，manifest 只记录当前 visibility，不视为冲突。通用 `DJI_...` 命名以本项目的 Pocket 3 输入范围解释；若 metadata 明确显示其他 DJI 机型，则报冲突。照片中的其他相机型号不会被改成 ONE RS。

支持 INSV 入库不代表支持拼接，也不保证 raw 双鱼眼视频能作为正常全景播放；master 的原字节仍由 Immich 管理。

## Metadata 和 XMP

1. 读取媒体的 Make / Model / Lens 字段。dry-run 和 report 中每个文件都显示 `embedded`（文件自带）与 `expected`（导入后验证的目标值），以及是否生成 XMP。
2. **照片尊重原始 EXIF。** JPG / JPEG / DNG / INSP 只要自带 Make 和 Model（且与文件名判断的机型不冲突），就原样保留、不生成 XMP，验证时以自带值为准。只有缺少 Make / Model 的照片才写标准 XMP。
3. **XMP 用于给视频补充相机信息。** MP4 / INSV 的 embedded 值与目标字符串（`DJI OsmoPocket3`、`Insta360 OneRS` 加镜头）不一致时生成 XMP；已一致则不生成。
4. `tiff:Make` / `tiff:Model` 保存指定字符串；镜头用 **`exifEX:LensModel`**。
5. 小型 XMP 在内存中生成，作为 multipart `sidecarData` 和原媒体 `assetData` **同一请求上传**。XMP part 文件名是 `original.ext.xmp`。媒体由文件句柄流式发送，不复制到 staging，也不整体载入内存。
6. API 读取 asset，验证 owner、SHA-1、managed-library 身份、visibility 和相机字段；请求 `refresh-metadata`，限时轮询到期望值。

**RAW + JPG 成对导入。** 同一目录下同名的 `JPG/JPEG/INSP` 与 `DNG` 视为同一张照片：两者都上传到 timeline，然后调用 `POST /stacks` 组成一个 Immich stack，JPG 为主图，DNG 作为堆叠版本。Immich 时间线只显示 stack 主图，所以不会出现重复；打开照片可切换到 RAW。重复运行会核对已有 stack；如果 RAW 或 JPG 已在别的 stack 里，报 `STACK CONFLICT` 并失败，不自动拆并。单独的 DNG（没有同名 JPG）按普通照片进 timeline。API key 需要 `stack.create`、`stack.read` 权限。

源目录中的已有关联 XMP 会安全解析并合并，只更改要求标准化的相机属性，其他属性保留。同时存在两种候选命名、多个媒体共用一个 sidecar、无效 XML 或过大的 XMP 会报错。孤立 XMP 仍是 UNKNOWN。

**Lens 的特别处理：** Immich 3.1.0 优先读取 `LensID → LensType → LensSpec → LensModel`。ExifTool 会把填入文本的 `aux:LensID` 转成 `Unknown (...)`，因此本工具不会这样写。如果源文件已有会遮盖目标 LensModel 的冲突字段，则报告 NEEDS REVIEW；不伪造数字 ID。服务端验证不符合目标值时始终失败。

**已有 asset 不补传 XMP。** 重复导入会读取并验证已有 metadata，修复 visibility，必要的 extraction refresh 可以重跑。已有 metadata 不正确、XMP 缺失或不生效时，返回非零；不删除重建 asset、不直接操作数据库或 managed storage。

`GET /assets/{id}` 不提供独立的 sidecar 内容回读接口。本工具的新上传依赖 v3.1.0 官方 multipart sidecar 持久化路径，并验证最终 metadata。API 任务异步执行，轮询正确字段不等于提供独立的任务完成收据；工具不会声称下载核验过服务器端 XMP。完整接口依据见 [实现说明](docs/immich-3.1.0.md)。

## 配置

优先级：**命令行 > 环境变量 > config JSON > 默认值**。

默认读取 `~/.config/camera-import/config.json`；可用 `--config PATH` 覆盖。每次运行开头都会打印生效的配置（config 文件、companion / manifest 目录、工具路径、API 地址来源、账号），不包含 API key；`--json` 模式下这些信息放在 report 的 `config` 字段。运行前可用 `camera-import --check-config` 验证配置和 Immich 连接：

```sh
camera-import --check-config
camera-import --check-config --companion-root /volume1/immich/companions --manifest-root /volume1/immich/manifests
```

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

账户需具备相应的 API 权限：`user.read`、`server.about`、`asset.statistics`（CLI server-info）、`asset.upload`、`asset.read`、`asset.update`、`job.create`（metadata refresh）、`stack.create`、`stack.read`（RAW + JPG stack）。以服务器的权限设置为准；权限不足明确失败。Storage Label `camera` 和 Storage Template 由 Immich 自己设置和维护。

| 选项 | 行为 |
|---|---|
| `[PATH]` | 默认 `.` |
| `--dry-run` | 只读本地计划；不连接 API、不创建输出目录、不归档、不写 manifest |
| `--check-config` | 只检查配置：输出目录、ExifTool、Immich CLI、凭据、服务器版本 / 账号 / 权限；不读媒体、不创建目录 |
| `--verbose` | stderr 输出分类、hash、无 secret 的 API 状态 |
| `--quiet` | 关闭 stderr 上的进度行 |
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
- 默认已知素材继续处理，但 unknown、不完整 bundle（缺少 master）或任何错误仍导致最终非零退出。
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
