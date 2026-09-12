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

安装器自动安装 **camera-import、camera-repair-time、ExifTool 和 Immich CLI**，不需要 pip、sudo 或 root。已存在且可运行的工具会复用；不重新登录，不改动 API key、素材、companions 或 manifests。

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
| ONE RS `VID_..._00/10_....mp4`、`PRO_VID_..._00/10_....mp4`（HDR/PRO） | Immich timeline | Arashi Vision / Insta360 OneRS / 4K Boost Lens |
| ONE RS `LRV_..._01/11_....mp4`、`PRO_LRV_...mp4` | 明确忽略，源文件保留 | — |
| ONE RS `LRV_..._11_....insv`、`PRO_LRV_..._11_....insv` | Immich timeline；360 bundle stack 的主 asset | Arashi Vision / Insta360 OneRS / 5.7K 360 Lens |
| ONE RS `VID_..._00/10_....insv`、`PRO_VID_..._00/10_....insv` | Immich timeline；作为同一 stack 的成员，时间线上不单独显示 | 同上 |
| 明确可独立保存的 INSP / JPEG / DNG（含 HDR 包围曝光的多张 `IMG_` 原片） | Immich timeline；同名 JPG/INSP + DNG 组成一个 stack | 必须由 metadata 确认机型；保留照片自带 EXIF |
| `Thumb/` 内 JPG/JPEG/PNG/BMP/THM | 明确忽略，源文件保留 | 不把任意 Thumb 子文件都当缓存 |
| `@` 或 `.` 开头的目录（DSM `@eaDir`、`@Recycle`、`.hidden`） | 整个目录跳过，不进入、不分类 | 报告中列为 skipped |
| 其他文件、symlink、特殊文件 | UNKNOWN / NEEDS REVIEW | 最终失败，保留源文件 |

文件名不匹配已知规则、未来的新 channel、空文件、无效日期、疑似多文件照片都会要求检查。

**文件命名参考。** Insta360 ONE RS：`[PRO_]{VID|LRV|IMG}_YYYYMMDD_HHMMSS_{00,01,10,11}_XXX.{mp4|insv|insp|jpg|dng}`。两位数第一位是镜头（0 / 1），第二位 0 是 master、1 是 LRV 代理；`PRO_` 前缀表示 HDR / PRO 类模式（Active HDR、FreeFrame 等），Insta360 官方说明这类文件需要在 App / Studio 中后处理，本工具按同样的 channel 规则入库。4K Boost 镜头的 master 可能出现在 `00` 或 `10` channel，两者都写入 `4K Boost Lens`。HDR 照片在卡上是 3 张同一时间戳、序号连续的 `IMG_` 原片，逐张作为独立照片进入 timeline；合成需要 Insta360 App。DJI Pocket 3：`DJI_YYYYMMDDHHMMSS_NNNN_D.{MP4|JPG|DNG|WAV|AAC|LRF}`，AAC 是开启麦克风备份时与 WAV 同步生成的音频文件，与 WAV 同样归档。未在本表内的前缀（例如 X 系列的 `TIM_`、`INTV_`）仍报 UNKNOWN，确认后再补充。

**360 bundle 作为一个 Immich stack。** 一个 bundle 由 `VID_..._00`、`VID_..._10` 两个 master 和 `LRV_..._11` 代理组成。三个文件都以 timeline visibility 上传，然后组成一个 stack：LRV（可直接播放的低清版本）是主 asset，两个 master 是 stack 成员。Immich 时间线只显示主 asset，打开后可切换到 master 原始文件（双鱼眼未拼接画面）。不再使用 archive visibility。LRV 可由 master 重新生成，删除它不丢失原始数据，因此只缺 LRV 的 bundle 不算 incomplete：`VID_..._00` 改为主 asset，报告中以 `lrvMissing` 列出。缺少任一 master 仍是 INCOMPLETE 360 BUNDLE 并导致非零退出；只剩一个文件时不建 stack。之前按 archive 导入的 master 重新运行时会被修正为 timeline 并加入 stack。通用 `DJI_...` 命名以本项目的 Pocket 3 输入范围解释；若 metadata 明确显示其他 DJI 机型，则报冲突。照片中的其他相机型号不会被改成 ONE RS。

支持 INSV 入库不代表支持拼接，也不保证 raw 双鱼眼视频能作为正常全景播放；master 的原字节仍由 Immich 管理。

## Metadata 和 XMP

1. 读取媒体的 Make / Model / Lens 字段。dry-run 和 report 中每个文件都显示 `embedded`（文件自带）与 `expected`（导入后验证的目标值），以及是否生成 XMP。
2. **照片尊重原始 EXIF。** JPG / JPEG / DNG / INSP 只要自带 Make 和 Model（且与文件名判断的机型不冲突），就原样保留、不生成 XMP，验证时以自带值为准。只有缺少 Make / Model 的照片才写标准 XMP。
3. **XMP 用于给视频补充相机信息。** MP4 / INSV 的 embedded 值与目标字符串（Make `DJI` / Model `DJI OsmoPocket3`；Make `Arashi Vision` / Model `Insta360 OneRS` 加镜头）不一致时生成 XMP；已一致则不生成。Insta360 的 Make 采用相机在照片 EXIF 里写的公司名 `Arashi Vision`，使视频与照片一致。Pocket 3 的照片 EXIF Model 是产品代号 `PP-101`，视频 metadata 则是 `DJI OsmoPocket3`；两者都识别为 Pocket 3，照片按规则 2 保留 `PP-101`。
4. `tiff:Make` / `tiff:Model` 保存指定字符串；镜头用 **`exifEX:LensModel`**。
5. 小型 XMP 在内存中生成，作为 multipart `sidecarData` 和原媒体 `assetData` **同一请求上传**。XMP part 文件名是 `original.ext.xmp`。媒体由文件句柄流式发送，不复制到 staging，也不整体载入内存。
6. 每个 asset 上传后立即验证 owner、SHA-1、managed-library 身份和 visibility；相机字段在**全部上传完成后统一轮询**（`verify_timeout` 内），Immich 在此期间自行完成提取，不再每个文件单独等待。只有已存在且字段不符的 asset 才请求 `refresh-metadata`，新上传不额外触发重新提取。stack 依据已验证的 asset 身份建立，metadata 提取慢不会导致 bundle 漏 stack；超时的文件报 `Metadata verification timed out`，重跑即可完成验证。

**Stack 规则（RAW + JPG、360 bundle 共用）。** 同一目录下同名的 `JPG/JPEG/INSP` 与 `DNG` 视为同一张照片：两者都上传到 timeline，然后调用 `POST /stacks` 组成一个 Immich stack，JPG 为主图，DNG 作为堆叠版本；单独的 DNG 按普通照片进 timeline。360 bundle 同理，LRV 为主。所有成员上传并验证后才建 stack。重复运行会用 `GET /stacks/{id}` 核对：已有 stack 已包含全部成员则直接接受，不改主 asset（例如本地删除 LRV 后，服务器上的 stack 仍以 LRV 为主）；已有 stack 只由本次成员组成但缺少新成员（之前部分导入的 bundle）时，先 `DELETE /stacks/{id}` 解散再重建，解散不删除任何 asset；已有 stack 混有其他 asset 时报 `STACK CONFLICT` 并失败，不自动拆并。同一 stack 内两个成员字节完全相同（同一个 Immich asset）也报错。API key 需要 `stack.create`、`stack.read`、`stack.delete` 权限。

源目录中的已有关联 XMP 会安全解析并合并，更改要求标准化的相机属性；视频还写入经核对的拍摄日期，其他属性保留。同时存在两种候选命名、多个媒体共用一个 sidecar、无效 XML 或过大的 XMP 会报错。孤立 XMP 仍是 UNKNOWN。

**Lens 的特别处理：** Immich 3.1.0 优先读取 `LensID → LensType → LensSpec → LensModel`。ExifTool 会把填入文本的 `aux:LensID` 转成 `Unknown (...)`，因此本工具不会这样写。如果源文件已有会遮盖目标 LensModel 的冲突字段，则报告 NEEDS REVIEW；不伪造数字 ID。服务端验证不符合目标值时始终失败。

**已有 asset 不补传 XMP。** 重复导入会读取并验证已有 metadata，修复 visibility，必要的 extraction refresh 可以重跑。已有 metadata 不正确、XMP 缺失或不生效时，返回非零；不删除重建 asset、不直接操作数据库或 managed storage。

`GET /assets/{id}` 不提供独立的 sidecar 内容回读接口。本工具的新上传依赖 v3.1.0 官方 multipart sidecar 持久化路径，并验证最终 metadata。API 任务异步执行，轮询正确字段不等于提供独立的任务完成收据；工具不会声称下载核验过服务器端 XMP。完整接口依据见 [实现说明](docs/immich-3.1.0.md)。

## 修复已导入视频：camera-repair-time

独立命令，`sh install.sh` 同时安装；也可直接运行仓库的
`python3 camera-repair-time.py ...`，兼容 Python 3.8.15，无需 pip。

以**原素材目录**为范围，逐文件计算 SHA-1/SHA-256，通过官方 checksum 判重接口定位现有 asset。
需要原媒体仍可读取；不会扫描整个账户猜测匹配，不会上传不存在的 asset。
只处理本项目识别的 MP4/INSV master 和有保留价值的 360 LRV；普通 MP4 LRV 不处理。

默认连接 Immich 做只读预览（`--dry-run` 同义），显示旧时间、时区和目标值：

```sh
camera-repair-time --capture-timezone America/Los_Angeles /path/to/originals
camera-repair-time /volume1/immich/media/library/camera/2026/2026-09-09   # Immich 自己的库目录也可以
```

确认按文件名修复这些原始视频时，可显式选择 `filename`；加 `--apply` 才执行：

```sh
camera-repair-time --time-source filename --capture-timezone America/Los_Angeles /path/to/originals
camera-repair-time --apply --time-source filename --capture-timezone America/Los_Angeles /path/to/originals
camera-repair-time --match checksum /path/to/originals   # 强制按 checksum 匹配
```

处理对象是文件名带相机钟点的照片和视频（`DJI_…`、`VID_`/`LRV_`、`IMG_`，含 `PRO_` 变体），JPG / DNG / INSP 与 MP4 / INSV 一视同仁。
选日期的规则与导入完全相同（同一个 `capture_time.choose`）：文件自带可靠的、带时区的拍摄时间就用它，否则文件名钟点 + 配置时区。

`--apply` 对每个 asset 发一次 `PUT /assets/{id}`，Immich **同步**写入并锁定 `dateTimeOriginal` 和 `timeZone`，
PUT 成功即为修复完成（状态 `updated`，响应里带回的字段会顺带核对，不做额外轮询）。
之后是 Immich 自己的异步工作：`SidecarWrite` 写 XMP，完成后 `AssetExtractMetadata` 刷新时间线用的 `localDateTime`，
如果 Storage Template 含日期，文件还会被移到新日期的目录。所以在 Immich 库目录上运行后，源文件很快会不在原处；
工具在 apply 后不再复查源文件。重跑时已修好的 asset 显示 `already_correct`。

**相机时钟设错了：** 用 `--clock-shift` 给整批文件加同一个修正量。修正作用在相机自己的钟点上（文件名或 metadata），然后再按拍摄时区解释，所以跨夏令时也正确。
修正量 = 真实时间 − 相机显示时间，格式 `天d时:分:秒`，也接受 `-1h30m`、`45s`、`P221DT34M12S`：

```sh
# 相机记的 2026-01-27 17:19:48 实际是 2026-09-05 17:54:00 → 差 221 天 34 分 12 秒
camera-repair-time --clock-shift 221d00:34:12 /volume1/immich/media/library/camera/2026/2026-01-27
camera-repair-time --apply --clock-shift 221d00:34:12 /volume1/immich/media/library/camera/2026/2026-01-27
camera-repair-time --apply --clock-shift 221d00:34:12 --only "*.insv" --only "VID_202601*" /some/folder
```

`--only GLOB` 只处理文件名匹配的文件，避免碰到同一目录里日期本来就对的文件。GLOB 是 shell 通配符语法：`*` 任意长度、`?` 单个字符、`[abc]` 字符集，
只匹配文件名（不含目录），区分大小写；可以重复给多个，命中任一即处理。记得加引号，否则 shell 会先展开。
报告里 `clockShift` 记录修正量，每个文件的 `timeSource` 也会注明。`camera-import --clock-shift …` 对导入同样有效（仅本次运行，不写入 config）。

asset 匹配顺序（`--match auto`）：1）hash 索引命中（同一文件之前处理过）；2）Immich 里 originalFileName、字节大小、类型都相同且唯一的 asset，
用 `POST /search/metadata` 查找，不读文件。这是对 Immich 库目录（`/volume1/immich/media/library/...`）做修复时的常态，
因为对 Immich 自己保存的文件再算 hash 只是拿 Immich 的副本和它自己比较；3）以上都不成立（没找到或同名同大小不唯一）时读文件算 hash，
按 checksum 匹配。报告里每个文件的 `hashSource` 标明用了哪一种。`--match checksum` 强制第 3 种。

```sh
```

`--time-source auto`（默认）复用新导入的可靠带时区 metadata → 文件名规则；
`filename` 明确忽略媒体内日期，使用文件名和配置的拍摄时区。
此修复工具读取原视频，不使用旧 source XMP 的日期作为修复依据，也不修改该 XMP。
同一 bundle 统一目标日期；目录只有部分 bundle 成员时仅修复目录内存在的成员，不会自动扩展到 stack 其他成员。
不同拍摄时区的素材应按各自目录分别处理。

执行前核对 owner、checksum、VIDEO 类型、managed-library 身份以及非 trash/offline 状态。
Unknown、匹配不到 asset、时区缺失、目标冲突等任一计划错误都会阻止整批修改。
已正确的日期跳过；重复字节只修改同一 asset 一次。网络中断可重跑，不创建重复资源。
修改前会重新核对服务器状态，避免覆盖预览期间发生的更改。

修复调用 **`PUT /assets/{id}`，只传 `dateTimeOriginal`（带显式偏移）**。
Immich 从日期提取时区，并自动排队 `SidecarWrite`，将日期写入服务器端 XMP；
写入成功后服务端自动触发 metadata extraction，刷新时间线日期。
不会删除重传、操作数据库或 managed storage；asset ID、stack、visibility、相机信息和 GPS 不由工具更改。
工具验证日期的绝对时刻、当地钟点、时区偏移，并检查上述非日期字段未改变。
API 权限除原先的连接/校验权限外需要 `asset.update`；不要求 `job.create`，不发起 metadata refresh。

**验证边界：** API 返回成功及日期匹配，不等于异步 XMP 写入任务已经完成。
此版本没有独立 sidecar 完成回执/回读接口，所以报告明确标记
`queued_by_immich_not_independently_verified`，不声称已逐份核验服务器 XMP。
源媒体永远不写入。

`--apply` 在首次修改前及每个文件处理后原子保存审计记录：

```text
$MANIFEST_ROOT/time-repairs/<UTC-time>-<unique-id>.json
```

包含 source、asset ID、checksum、目标时间、旧/新时间和逐项状态，不含 API key。
无法保存初始审计记录时不会开始修复。执行过程失败可能已完成部分修改，返回非零并保留记录；不自动回滚。
只读预览不写报告或其他持久文件。支持 `--json`、`--quiet`、`--config`、`--manifest-root`、
`--verify-timeout` 以及已有环境变量配置。这个工具不输出任何可格式化源卡的结论。

## 视频拍摄时间和时区

MP4 / INSV 使用可靠且带偏移的 `DateTimeOriginal` / `CreationDate`（也支持
`DateTimeOriginal` + `OffsetTimeOriginal`）；没有可靠带时区日期时，使用已识别的相机文件名时间。
QuickTime 整数 `CreateDate` / `MediaCreateDate` 的 UTC 语义不能仅凭字段名保证，因此只记录用于诊断，
不直接当作带时区的拍摄日期。文件系统 mtime / ctime 不用作视频拍摄时间回退。

**文件写下的时间是权威的，导入和修复同一份实现（`capture_time.choose`）。** 三个正交的量：① 相机钟读数（文件名或 EXIF 钟点，只有 `--clock-shift` 会改它）；② 相机钟所在时区（优先文件自证，其次 `--clock-timezone`，再次 `--capture-timezone`）；③ 拍摄地时区 `--capture-timezone`（已知时刻后一律换算到它显示，config 里存常驻拍摄地，出国目录用命令行覆盖）。按证据强弱依次判断：

1. 带时区的日期（`DateTimeOriginal` / `SubSecDateTimeOriginal` 配 `OffsetTimeOriginal`，或带时区的 `CreationDate`）：直接采用，导入时不写日期 XMP，由 Immich 自行提取。
2. 不带时区的 EXIF `DateTimeOriginal`（EXIF 2.31 之前的照片，如 Pocket 3、ONE RS 的 JPG / DNG / INSP）：**原样尊重，不做任何解释**，配置的时区对它无效；修复工具显示 `exif_respected`。Immich 会把它当 UTC 存，墙上时间正确。
3. 视频的 QuickTime `CreateDate` 是 UTC，与文件名里的本地钟点之差是文件自己证明的时区（取整到 15 分钟、允许几秒抖动、±14 小时内、非零）：按此偏移写入，不需要任何配置。DJI 视频属于这种情况。
   有这种 UTC 证据时，**时刻来自文件，墙上时间由配置的拍摄时区决定**：相机显示的钟没进夏令时、或出国后仍显示家里时间，`CreateDate` 依然正确，配置了当地时区（例如 `--capture-timezone Asia/Tokyo`）就能得到真实当地时间，来源注明 `camera clock showed -07:00`。没配置时按相机显示的偏移展示，时刻同样正确。
4. 只剩文件名钟点时（相机把本地时间写进了 UTC 字段，或视频没有 `CreateDate`）：用配置的拍摄时区解释；没配置就留给 Immich，不报错，报告 `captureSources` 里计为 `no timezone evidence`，修复工具显示 `no_timezone_evidence`。

**没有 UTC 证据的相机（Insta360 的 `CreateDate` 写的是本地时间）出国拍摄且没改相机时区时**，用 `--clock-timezone` 说明相机的钟显示的是哪个时区，再用 `--capture-timezone` 给出拍摄地：
`--clock-timezone America/Los_Angeles --capture-timezone Asia/Tokyo` 把文件名的 17:00 理解为洛杉矶时间，再换算成东京当地时间显示。

**唯一会改动文件所写钟点的是显式的 `--clock-shift`**（相机时钟设错）：它加在相机钟点上，对以上四种情况一律生效；不带时区的 EXIF 移位后仍不带时区。
`--time-source filename` 是另一种显式覆盖：无视 metadata 里的钟点，只按文件名 + 配置时区处理。

属于第 4 种情况的视频需要配置拍摄时区，例如：

```sh
camera-import --capture-timezone America/Los_Angeles --dry-run /some/folder
camera-import --capture-timezone America/Los_Angeles /some/folder
```

不需要每次在命令行指定：把常驻拍摄地写进 config JSON 的 `capture_timezone`（或环境变量 `CAMERA_CAPTURE_TIMEZONE`），
`camera-import` 和 `camera-repair-time` 都会读取；只有在别处拍摄的目录才需要用 `--capture-timezone` 覆盖。
优先级仍是命令行 > 环境变量 > config。带时区 metadata 的文件不受此影响，配置值只用于文件名钟点。
默认不猜时区；没有配置时这类视频原样交给 Immich（当 UTC 显示），不阻止上传。配置只补文件自身没有的信息，从不覆盖 EXIF。
IANA 时区依据**拍摄日期**处理夏令时。DSM 既没有 `/usr/share/zoneinfo` 数据库，其 Python 3.8 也没有编译 `time.tzset`，
因此夏令时计算完全用纯 Python 的 POSIX TZ 规则求值器完成，不依赖 libc 和 pip。规则来源依次为：系统 zoneinfo 文件的 footer
（存在时，`TZDIR` 可指定目录）、脚本内置的约 500 个 IANA 时区当前规则表（取自 tzdata 的 TZif footer）、或直接给出的
POSIX 规则，例如 `PST8PDT,M3.2.0,M11.1.0`。规则表是各时区最近一次变更后的规则，更早的夏令时历史（例如美国 2007 年前）
和 tzdata 里以逐年显式转换记录的例外（摩洛哥的斋月）不在其中。测试用 Python 自带 zoneinfo 对全部时区逐月核对求值器。
重复/不存在的夏令时钟点需要显式偏移，例如 `--capture-timezone=-07:00`。拍摄地不同的目录应使用各自时区，不应以 NAS 或当前浏览器时区推断。

同一 360 bundle 共用一个拍摄时间及偏移：优先共享成员中可靠的时间，其他成员从中补齐；
所有成员都没有时使用共同的文件名。可靠成员日期/偏移冲突，或与文件名钟点相差超过 2 秒时要求 review。
现有 source XMP 中日期必须带明确偏移，并参与上述核对；不会默默忽略人工编辑的 sidecar 日期。

每个视频在内存中的 XMP 写入带偏移的 `exif:DateTimeOriginal`，随新 asset 一起上传。
`fileCreatedAt` 同时使用该拍摄时刻，`fileModifiedAt` 仍为源 mtime。
上传后验证 `exifInfo.dateTimeOriginal` 的绝对时刻、`localDateTime` 的当地钟点和
`exifInfo.timeZone` 对应的偏移，允许等价时区字符串。任何不一致均失败。
报告包含 `captureTime`、`captureTimeSource` 和原始 `embeddedTimeMetadata`。
此版本时间增强仅针对视频；照片维持原有 EXIF 策略。

**已导入资源仍不补传 XMP。** 重跑会验证日期；已有错误日期会失败，不会自动修好或删除重传。

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
  "capture_timezone": "America/Los_Angeles",
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

账户需具备相应的 API 权限：`user.read`、`server.about`、`asset.statistics`（CLI server-info）、`asset.upload`、`asset.read`、`asset.update`、`stack.create`、`stack.read`、`stack.delete`（RAW + JPG 与 360 bundle stack）。`job.create`（metadata refresh）可选：缺少时报 WARNING，改为直接验证 Immich 上传后自行提取的 metadata。以服务器的权限设置为准；其他权限不足明确失败。API 错误会附带服务器返回的 `message`（如 `Missing required permission: ...`、`Quota has been exceeded!`）。Storage Label `camera` 和 Storage Template 由 Immich 自己设置和维护。

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

- WAV / AAC 先写目标目录内临时文件（源只读一遍，边复制边算 hash），fsync 后重新读取副本核验 SHA-256，再用不覆盖已有文件的原子操作发布。已有同 hash 文件跳过，不同 hash 永不覆盖。日期取 filename，不取 mtime。
- 媒体同时计算 SHA-1（Immich checksum）和 SHA-256（manifest）。**新文件只读一遍：** hash 直接从上传的数据流计算，上传后与服务器保存的 checksum 比对。相同 checksum 对应冲突角色或相机信息时，不尝试反复修改同一个 asset；这类冲突在 hash 已知时上传前拦截，否则在第一份上传后发现并把相关文件全部标为失败。
- **Hash 索引 `manifest_root/hashes.json`：** 以文件 fingerprint（设备、inode、大小、mtime、ctime）为键记录 SHA-1 / SHA-256。重跑时 fingerprint 一致的文件不再读盘，直接用 `bulk-upload-check` 判重；索引只回答「这些字节上次算出来是什么」，是否已入库和完整性仍由服务器 checksum 决定。每算完一个文件的 hash 就立即写入索引（原子替换一个几 KB 的 JSON），所以 Ctrl-C、SSH 断线被杀、断网、响应丢失后重跑都不会重复读已经处理过的文件。180 天未再见到的条目自动删除，总数上限 20 万条，每条约 200 字节。
- 只有 manifest 里已经记录过的 360 bundle 成员会在上传前单独读一遍算 hash，用来在上传前发现「同名不同内容」的 manifest 冲突。
- Report 的 `hashSources` 统计每个文件的 hash 来源：`index`（索引命中）、`upload`（上传流）、`read`（上传前读取）。
- Manifest 记录 bundle key、role、原 filename、size、SHA-256、Immich checksum、asset ID、owner 和服务器身份。锁定后检查旧记录，合并缺失信息，再原子写入；冲突保留原文件并失败。
- 允许保存不完整 bundle 的部分进度，但不会把它算作安全完成。重复运行能补齐缺失成员的 asset ID，并把 stack 重建为完整成员。
- 网络中断后直接重复执行同一命令即可通过 checksum 找到已有资源；不依赖上次报告决定跳过验证。
- 默认已知素材继续处理，但 unknown、不完整 bundle（缺少 master）或任何错误仍导致最终非零退出。
- 正常运行结束重新检查源文件状态及目录清单。发现新文件、文件变化或扫描失败，不能给出成功结论。文件身份以大小和 mtime 为准：FAT / exFAT 的 USB 挂载在 Linux 上 inode 号不稳定（按需分配、缓存回收后重新编号）、ctime 是合成值，这些字段只用于报错时的诊断信息（如 `SOURCE CHANGED: ... (inode 123->456)`）。
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
