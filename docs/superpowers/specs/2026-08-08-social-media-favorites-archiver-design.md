# Social Media Favorites Archiver 设计方案

- 项目名称：`social-media-favorites-archiver`
- 展示名称：Social Media Favorites Archiver｜社交媒体收藏归档
- 设计日期：2026-08-08
- 修订版本：v2（外部审查修订）
- 状态：已根据外部 Agent 审查完成修订，并已拆分为可执行实施计划
- 首要平台：Bilibili、小红书（Xiaohongshu/RedNote）、抖音（Douyin）
- 计划许可证：应用源码 MIT；可独立分发的 Skill bundle 使用 MIT-0，以符合 ClawHub 发布条款

## 1. 摘要

本项目提供一个面向 Agent 的开源 Skill，把用户在 Bilibili、小红书和抖音中的个人收藏自动同步到本地，并将视频、图文笔记及其元数据整理成可由 Obsidian 直接使用的 Markdown 知识库。

系统复用用户已经授权的浏览器登录状态，先快速枚举收藏并生成可浏览的 Markdown 骨架，再通过持久化任务队列补充下载、语音识别、OCR、文本融合和摘要。Bilibili 先探测原生字幕；小红书和抖音按“多数视频需要本地 ASR”的容量模型设计，同时对画面烧录字幕和其他视觉文字执行 OCR。原始正文、字幕、OCR 结果和媒体附件保存在本地；只有提取后的文字可以发送给用户配置的大模型。原始视频只作为临时处理材料，所有派生产物校验成功后删除。

项目以“可维护的适配器 + 可恢复的本地流水线”为核心，而不是把三个平台的页面操作写成一个单体脚本。平台页面变化时，只需维修对应适配器；转写、OCR、Markdown 输出和任务状态不受影响。

## 2. 已确认的产品决策

1. 三个平台均支持登录态下的收藏列表自动同步，不要求用户逐条粘贴链接。
2. 最终知识库保存在本地，格式为 Markdown 与本地附件，可由 Obsidian 直接打开。
3. 默认保留文字稿、原始图文图片、封面、必要的视频关键帧和来源元数据。
4. 原始视频在转写、渲染和文件校验成功后删除，不作为长期档案保存。
5. 下载、字幕提取、语音识别和 OCR 在本地执行。
6. 大模型是可选增强层，只接收提取后的文字，不接收视频、音频、Cookie 或浏览器会话。
7. 用户取消收藏时不删除本地笔记，只更新来源状态。
8. 首版面向个人账号和个人知识管理，不提供批量爬取公共内容、绕过验证码或内容再发布能力。
9. 同步采用“轻量枚举与骨架输出 + 持久化重处理队列”的两段式流程。
10. 项目目标仍是三个平台完整支持，但稳定发布按 Bilibili、小红书、抖音顺序逐步推进。

## 3. 目标与非目标

### 3.1 目标

- 自动发现三个平台收藏夹中的新增或更新内容。
- 对重复运行保持幂等，不重复下载、转写或生成同一条内容。
- 同时处理视频、纯文字、图文笔记和多图内容。
- 生成稳定、可搜索、可迁移且不绑定 Obsidian 私有格式的 Markdown。
- 在网络中断、登录过期、平台限流或单条内容失败后安全恢复。
- 保护用户手工添加到 Markdown 中的笔记，重新同步时不覆盖用户编辑区。
- 让其他 Agent 能通过中文或英文需求准确发现并调用本 Skill。
- 将平台变化限制在独立适配器中，降低长期维护成本。

### 3.2 非目标

- 不绕过验证码、设备验证、付费墙、访问控制或平台风控。
- 不做面向陌生账号的大规模抓取或商业数据采集。
- 不归档评论区、弹幕和直播内容；这些可以在未来作为可选扩展。
- 不永久保存原始视频，也不把本项目设计成媒体播放器。
- 不自动发布、转发、点赞、评论或修改用户平台账号。
- 不把 Notion、云盘或远程数据库作为首版的主存储。
- 不保证平台私有接口永远稳定；系统通过诊断、暂停和适配器升级来管理变化。

## 4. Agent 发现与 Skill 触发设计

Skill 目录名使用明确的任务词，而不是抽象品牌名。以下 metadata 是 1.0 三平台 stable 的目标描述；0.1～0.3 若对外发布 Skill，必须只在 description 中声明已经达到 stable 的平台，并把其他平台明确标为 experimental，不能提前宣称稳定支持：

```yaml
---
name: social-media-favorites-archiver
description: >
  Sync and archive a user's own saved or favorited collections from supported
  social-media platforms—Bilibili, Xiaohongshu/RedNote, and Douyin—into a
  local Markdown or Obsidian knowledge base, including transcription and OCR
  needed to make those saved collections searchable. Use when the request is
  specifically about backing up, migrating, or organizing personal favorites,
  saved posts, collections, bookmarks, saved videos, or saved image notes.
  适用于整理或备份用户本人的 B站、小红书或抖音收藏，或把这些收藏中的
  视频、图片和图文笔记转换为本地 Markdown/Obsidian 知识库。不要用于
  单个链接的一次性下载、普通视频总结、单张图片 OCR、营销创作或内容发布。
---
```

触发设计遵循以下原则：

- 名称表达核心对象和动作：social media、favorites、archive。
- 描述同时覆盖平台名、收藏语义、内容类型和输出目标。
- 同时包含中文与英文常见说法，兼顾语义检索和关键词检索。
- 所有动作都锚定在用户本人收藏、保存内容或收藏夹同步上，不能仅凭 summarize、OCR、extract text 等宽泛动词触发。
- 避免在名称中绑定三个具体平台，以便未来扩展 YouTube、TikTok、微博或其他来源。
- 在发布前使用正向和负向提示词测试触发准确度，防止“所有社交媒体任务”都误触发本 Skill。

## 5. 系统架构

```text
Agent / 用户
    │
    ▼
SKILL.md 与 Agent 工作流
    │
    ▼
本地 CLI / Orchestrator
    ├── Bilibili Adapter
    ├── Xiaohongshu Adapter
    └── Douyin Adapter
    │
    ▼
标准化内容模型 + SQLite 任务状态
    │
    ▼
Markdown 骨架立即可见
    │
    ▼
持久化重处理队列
    ├── 原生字幕/正文提取
    ├── 本地 ASR
    ├── 图片与自适应视频帧 OCR
    ├── ASR × OCR 时间轴融合
    └── 可选文字摘要与标签
    │
    ▼
Markdown Renderer + Assets
    │
    ▼
校验成功 → 清理临时视频
```

系统分为五层：

1. **Skill 层**：告诉 Agent 何时调用、如何检查环境、如何登录、如何同步、何时需要人工介入。
2. **平台适配层**：只负责认证状态、收藏枚举、详情提取和素材定位。
3. **核心编排层**：负责两段式同步、持久化队列、幂等、重试、状态迁移、配置和运行报告。
4. **内容处理层**：负责字幕、ASR、OCR、时间轴融合、摘要和结构化内容生成。
5. **存储与输出层**：负责 SQLite、骨架/完整 Markdown、附件、索引、日志和安全清理。

每一层通过明确的数据对象交互。平台适配器不得直接写 Markdown；处理器不得知道平台页面结构；渲染器不得负责下载媒体。

## 6. 建议技术栈

- 运行时：Python 3.11 及以上。
- 包与环境管理：`uv`，同时保持标准 `pip` 安装兼容性。
- CLI：Typer。
- 数据校验：Pydantic。
- 本地数据库：SQLite，启用 WAL；使用标准库 `sqlite3` 和薄 repository/migration 封装，不引入 ORM。
- HTTP：`httpx` 只处理公开接口、已取得的静态资源 URL 和可安全重试的媒体下载；不在 Python 中自行计算平台私有接口签名。
- 浏览器：Chrome/Chromium 登录配置文件；采用 Playwright 通过 CDP 连接真实浏览器。收藏列表和详情等私有数据由页面触发并拦截结构化响应，或在已登录页面上下文中发起请求。
- Bilibili 下载：直接封装 `yt-dlp` 的 Bilibili extractor、收藏夹和浏览器 Cookie 能力，不自研播放地址、WBI 或字幕链路。
- 小红书与抖音下载：收藏枚举由浏览器上下文完成；图片、视频等静态素材在取得可验证 URL 后交给受限下载器。
- 音视频处理：FFmpeg。
- 中文语音识别：本地 FunASR Paraformer/SenseVoice 兼容模型为默认；支持 ASR 热词和转写后领域词典纠错。
- Apple Silicon ASR 后备：`mlx-whisper` 或 `whisper.cpp`；首版不把只能使用 CPU 的 `faster-whisper` 作为 macOS 默认后备。
- Linux/NVIDIA ASR 后备：可配置 `faster-whisper`；具体默认值以代表性样本基准测试决定。
- OCR：RapidOCR + ONNX Runtime 为 macOS 默认后端；PaddleOCR 不作为 0.1 必需依赖。OCR 术语改进通过后处理词典完成，不宣称模型具备 ASR 式热词偏置。
- 摘要接口：OpenAI-compatible 文本接口，提供完全关闭选项。
- 测试：pytest、录制并脱敏的平台 fixture、少量受控端到端账号测试。

首版优先支持 macOS，其次支持 Linux。Windows 支持不作为 0.1 发布阻塞条件。

## 7. 仓库结构

```text
social-media-favorites-archiver/
├── AGENTS.md
├── README.md
├── LICENSE
├── pyproject.toml
├── skill/
│   └── social-media-favorites-archiver/
│       ├── SKILL.md
│       ├── LICENSE
│       ├── .clawhubignore
│       └── references/
│           ├── configuration.md
│           ├── platform-bilibili.md
│           ├── platform-xiaohongshu.md
│           ├── platform-douyin.md
│           └── troubleshooting.md
├── src/social_media_favorites_archiver/
│   ├── cli.py
│   ├── config.py
│   ├── orchestrator.py
│   ├── models.py
│   ├── state.py
│   ├── adapters/
│   │   ├── base.py
│   │   ├── bilibili.py
│   │   ├── xiaohongshu.py
│   │   └── douyin.py
│   ├── processors/
│   │   ├── subtitles.py
│   │   ├── asr.py
│   │   ├── ocr.py
│   │   ├── keyframes.py
│   │   ├── fusion.py
│   │   ├── terminology.py
│   │   └── enrichment.py
│   ├── storage/
│   │   ├── database.py
│   │   ├── assets.py
│   │   ├── markdown.py
│   │   └── indexes.py
│   └── safety/
│       ├── cleanup.py
│       ├── redaction.py
│       └── paths.py
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   └── fixtures/
├── evals/
│   └── evals.json
└── docs/
    └── superpowers/
        ├── specs/
        └── plans/
```

应用与 Skill 采用同一仓库、两个分发边界：Python 应用从仓库根目录按 MIT 发布；Agent Skill 从 `skill/social-media-favorites-archiver/` 独立安装与发布，并按 ClawHub 的分发约束使用 MIT-0。嵌套目录也让 Skills.sh 和 ClawHub 只安装轻量 Skill，而不是把源码、测试和验证材料复制进 Agent 的 Skill 目录。

## 8. 平台采集设计

### 8.1 统一适配器接口

每个平台实现相同的抽象能力：

```text
check_session() -> SessionStatus
begin_login() -> LoginInstruction
list_collections(cursor) -> Page[Collection]
list_favorites(collection, cursor) -> Page[FavoriteRef]
fetch_item(ref) -> NormalizedItem
download_assets(item, temp_dir) -> list[LocalAsset]
diagnose(error) -> DiagnosticBundle
```

适配器返回标准化数据，不向下游暴露平台响应结构。分页游标由适配器解释，核心层只保存和回传。

### 8.2 登录与会话

- 首次使用时，CLI 打开或连接专用浏览器配置文件，由用户完成扫码、设备确认等平台要求的登录步骤。
- 项目不保存账号密码。Cookie、Local Storage 和设备会话由浏览器配置文件管理。
- 浏览器配置文件默认位于用户数据目录，权限设为仅当前用户可读写，且永不放入 Git 仓库。
- 日常同步复用登录状态。会话过期时，任务进入 `needs_auth`，对应平台暂停，其他平台继续运行。
- 遇到验证码或风控时不尝试绕过，保存安全的恢复点并提示用户完成平台要求的验证。

### 8.3 Bilibili

- 通过已登录页面发现用户收藏夹 ID；具体收藏列表、播放地址、格式协商、WBI 和字幕提取直接封装 `yt-dlp` 的 Bilibili extractor。
- 调用 `yt-dlp --cookies-from-browser` 或等价 Python API 复用用户授权的浏览器登录态，不把 Cookie 写入参数日志。
- 优先接收 `yt-dlp` 返回的平台字幕、视频元数据、UP 主、发布日期、收藏夹名称、封面和 BV/AV 标识。
- 无可用字幕时才下载足够用于转写的媒体；本项目不再实现独立的播放地址或字幕接口客户端。
- 收藏枚举结果、关系状态、任务队列、Markdown 和安全清理由本项目负责。
- 分 P 视频的每一 P 作为子章节处理，共享一个主收藏条目。

### 8.4 小红书

- 通过用户已登录的收藏页面枚举笔记；优先读取页面可见数据和浏览器已加载的结构化响应。
- 支持纯文字、多图、图文和视频笔记。
- 保存正文、作者、发布时间、收藏时间（可取得时）、标签、原图、视频封面和原始链接。
- 图片按展示顺序保存，优先使用页面或结构化响应实际提供的 `imageList[].urlDefault` 等高质量 URL，并通过尺寸、Content-Type 和内容哈希验证下载结果。
- 不硬编码依赖某个 CDN URL 拼接规则；只有页面确实只提供缩略图时才降级并记录素材质量。
- 视频不能假设存在机器可读字幕。容量规划按多数视频需要本地 ASR 计算，同时执行自适应画面 OCR；发现可用字幕时仍优先使用，以避免不必要的下载和转写。

### 8.5 抖音

- 通过用户已登录的收藏页面枚举作品和收藏夹。
- 保存作品文案、作者、发布时间、封面、原始链接和平台内容 ID。
- 先廉价探测平台可用字幕或文字轨，但容量规划按多数视频需要本地 ASR 计算。
- 视频默认同时进入音轨 ASR 和自适应画面 OCR，再由时间轴融合层去除口播与烧录字幕的重复内容，并保留非口播视觉标注。
- 图集作品按图文内容处理，不强制进入视频流水线。

### 8.6 平台变化处理

私有收藏数据采用“真实页面触发结构化响应或页面上下文请求优先、稳定 DOM 后备”的策略。项目不在 Python 中复刻 `x-s`、`a_bogus` 等易变签名算法；`httpx` 只接收适配器已经取得且允许独立下载的静态资源 URL。每个适配器维护：

- 可识别的页面/响应版本；
- 最小必要字段及其验证规则；
- 脱敏后的 contract fixture；
- 失败诊断说明；
- 独立版本号和兼容性测试。

如果关键字段缺失，适配器应失败并停止该平台批次，不能静默输出空内容。诊断包可以保存选择器摘要、响应 schema、页面版本和错误截图，但必须先移除 Cookie、Token、私信、推荐流及其他不相关隐私信息。

## 9. 标准化数据模型

核心内容对象至少包含：

```text
NormalizedItem
  canonical_id       # 例如 bilibili:BVxxx、xiaohongshu:<note-id>
  platform
  content_type       # video | article | image_post | gallery
  source_url
  title
  author
  author_url
  published_at
  first_seen_at
  last_seen_at
  source_availability # available | unavailable | deleted | restricted
  collections[]       # 当前有效关系的派生视图，不是状态真源
  original_text
  native_subtitles[]
  assets[]
  source_revision     # 平台提供的更新时间/版本标记（可取得时）
  metadata_fingerprint
  platform_metadata

ItemCollectionMembership
  item_id
  collection_id
  state               # active | removed
  first_seen_at
  last_seen_at
  removed_at
  last_complete_run_id
```

SQLite 至少包含以下逻辑表：

- `items`：标准化内容、来源可用性、平台版本标记和轻量元数据指纹。
- `collections`：平台收藏夹及其本地映射。
- `item_collections`：内容与收藏夹的多对多关系及独立成员状态；条目只有在所有成员关系都失效时才派生为“已取消全部收藏”。
- `assets`：素材来源、类型、哈希、本地路径和清理状态。
- `jobs`：处理阶段、尝试次数、下一次重试时间和最后错误。
- `runs`：每次同步的开始、结束、平台统计、收藏夹枚举完整性和最终结果。
- `enrichments`：摘要模型、提示词版本、结构化结果和文字输入哈希。
- `extractions`：ASR、OCR、融合结果的处理器版本、输入指纹和结果哈希。

平台原始元数据只保存必要字段；大型响应和含敏感信息的完整响应不进入长期数据库。

## 10. 增量同步与任务状态机

同步分为轻量枚举和重处理两个阶段。轻量阶段优先让整个收藏库可见：

```text
discovered
  -> metadata_ready
  -> skeleton_rendered
  -> heavy_queued
```

随后持久化队列逐项补全：

```text
heavy_queued
  -> assets_ready
  -> extracted
  -> fused
  -> enriched
  -> rendered
  -> verified
  -> cleaned
```

骨架笔记至少包含标题、作者、来源链接、封面、收藏夹和 `processing_status`，枚举完成后即可在 Obsidian 浏览。重处理可以在当前同步进程中继续执行，也可以由已安装的本地 worker 消费；任务状态始终持久化。失败不会回滚已经验证的阶段，而是记录在当前阶段并进入 `retryable`、`needs_auth`、`needs_user_action` 或 `permanent_failure` 子状态。

任务领取使用 SQLite 原子更新建立 `lease_owner` 和 `lease_until`。同一 `canonical_id` 同时只能有一个可写任务；渲染器再取得条目级文件锁后才能写 Markdown。worker 崩溃后租约超时，其他 worker 才能继续，避免骨架渲染与完整渲染并发覆盖。

幂等规则：

- `canonical_id` 决定条目身份。
- 枚举期使用 `source_revision` 和 `metadata_fingerprint` 判断是否需要获取详情；它们不能包含尚未生成的转写文本。
- 素材下载后单独计算 `asset_sha256`，只用于文件完整性、去重和处理器缓存，不能代替轻量更新判断。
- ASR、OCR 和融合结果分别使用“输入素材哈希 + 处理器/模型版本 + 配置哈希”作为缓存键。
- 相同图片只保存一次或建立复用引用。
- 摘要以“文字输入哈希 + 模型 + 提示词版本”作为缓存键。
- Markdown 以 frontmatter 的 `smfa_id` 作为身份；数据库路径只是可重建缓存。重复同步更新已有条目的自动生成区域，不创建重复文件。
- 同一平台同时只允许一个收藏枚举任务，防止游标和限流相互干扰。

增量枚举默认使用安全 early-stop 快路径：收藏页按近期顺序返回时，只有连续遇到可配置数量的已知且未变化条目、完成至少一个重叠窗口、页面未报告排序/总量异常时才提前停止。系统定期执行完整枚举，首次同步、收藏夹结构变化和疑似重排时强制完整枚举。

取消收藏 reconciliation 只能对“本次从第一页到结束游标完整成功”的单个收藏夹执行。`needs_auth`、限流、布局变化、网络中断或 early-stop 批次都不能把未出现关系标记为 `removed`。默认保留 Markdown 和附件；条目只有在所有 `item_collections` 关系都失效时才显示“已取消全部收藏”。

## 11. 内容处理流水线

### 11.1 临时下载

- 临时文件存放在应用专属缓存目录，不放入 Obsidian 知识库。
- 下载采用 `.partial` 临时名，完成并校验大小后原子重命名。
- 每个文件记录来源、预期用途、哈希和归属条目。
- 清理器只能删除数据库中登记且位于已验证缓存根目录下的文件。
- 原始视频只有在字幕/ASR、关键帧、OCR、时间轴融合、封面、Markdown 和所有需保留附件均达到终态并通过重新读取校验后才可清理；任一派生任务失败都必须保留原视频供重试。

### 11.2 字幕和 ASR

字幕探测仍按成本从低到高执行：

1. 平台原生字幕；
2. 媒体中已有的字幕轨；
3. 本地语音识别。

“字幕优先”只表示先执行廉价探测，不表示系统可以按高字幕覆盖率估算资源。Bilibili 在有字幕时跳过 ASR；小红书和抖音按多数视频需要 ASR 的容量、耗时和磁盘预算设计。

本地 ASR 前由 FFmpeg 提取单声道、16 kHz 音频。中文内容默认使用 FunASR Paraformer/SenseVoice 兼容模型；Paraformer 可配置领域热词。Apple Silicon 的多语言或失败后备使用 `mlx-whisper`/`whisper.cpp`，Linux/NVIDIA 可使用 `faster-whisper`。输出保留分段时间戳，并生成可阅读的连续文本。首版不要求说话人分离。

转写完成后可以应用用户领域词典进行确定性纠错，但必须同时保留原始识别文本或变更记录，避免不可追溯地改写原意。

字幕或 ASR 必须通过以下检查，才能把对应语音提取任务标记为完成：

- 输出非空，或 VAD/音轨检查明确记录为 `verified_no_speech`；
- 音频时长与处理记录一致；
- 分段时间戳单调且未超过媒体时长；
- 文字稿已写入数据库。

这些检查只是清理屏障的一部分，不能单独触发原视频删除。

### 11.3 OCR

- 小红书与抖音图集的每张原图执行 RapidOCR，并保留图片顺序、尺寸和识别置信度。
- 视频同时使用低频定时采样和场景切换产生候选帧，再检测文字区域变化并使用感知哈希去重。不能只依赖场景切换，因为静止画面上的烧录字幕也会持续变化。
- 对高度相似、文字区域未变化的候选帧去重，避免重复 OCR 和大量无意义截图。
- OCR 结果按素材文件分组，保留顺序和可追溯引用。
- 低置信度文本保留但标注置信度，不与作者正文混合成不可区分的内容。
- OCR 不宣称支持 ASR 式热词偏置；领域词汇增强使用结果后处理、模糊匹配和可审计替换记录。

### 11.4 ASR × OCR 时间轴融合

视频 ASR 分段和视频帧 OCR 统一转换为：

```text
TextSegment
  start_time
  end_time
  text
  source          # native_subtitle | asr | burned_caption | visual_annotation
  confidence
  asset_ref
```

融合器按时间窗、规范化文本相似度和持续时长处理：

1. 同一时间窗内 ASR 与烧录字幕高度相似时，合并成一个主文字段，记录两个来源和最高置信度。
2. OCR 文本与音轨不重合时，作为 `visual_annotation` 保留，例如标题、价格、步骤、代码、地点或补充说明。
3. 连续帧中的相同烧录字幕合并时间范围，不重复输出。
4. 不确定的冲突不自动丢弃，分别保留并标注来源。
5. 融合结果、原始 ASR 和原始 OCR 都持久化，Markdown 默认展示融合稿，同时允许展开来源稿。

首版使用确定性相似度和时间窗规则，不依赖云端大模型完成去重。融合阈值必须通过“口播＋满屏字幕”“无口播教程”“静止画面换字幕”等样本测试。

### 11.5 大模型增强

大模型输入只包含以下文字字段：标题、作者正文、字幕、OCR 文本和必要的来源元数据。禁止发送 Cookie、登录状态、本地绝对路径、原始媒体和平台原始响应。

模型返回经过 schema 校验的结构化结果：

```text
summary
key_points[]
topics[]
tags[]
language
safety_notes[]
```

大模型不可用、额度不足或输出校验失败时，系统仍然生成包含完整原文的基础 Markdown，并将增强任务保留为可重试状态。

## 12. Markdown 与 Obsidian 输出

推荐知识库结构：

```text
KnowledgeBase/
├── Bilibili/
├── Xiaohongshu/
├── Douyin/
├── Indexes/
│   ├── Authors/
│   ├── Collections/
│   └── Tags/
├── assets/
│   ├── bilibili/<canonical-id>/
│   ├── xiaohongshu/<canonical-id>/
│   └── douyin/<canonical-id>/
└── .social-media-favorites-archiver/
    ├── archive.db
    └── logs/
```

Markdown 文件使用以下 frontmatter：

```yaml
---
smfa_id: "bilibili:BVxxxxxxxx"
platform: "bilibili"
content_type: "video"
source_url: "https://..."
author: "..."
published_at: "2026-01-01T12:00:00+08:00"
first_synced_at: "2026-08-08T12:00:00+08:00"
last_synced_at: "2026-08-08T12:00:00+08:00"
favorite_state: "active"
collections: ["稍后整理"]
tags: ["...", "..."]
smfa_generated_tags: ["...", "..."]
metadata_fingerprint: "sha256:..."
processing_status: "complete"
---
```

示例中的 `favorite_state` 是所有收藏夹成员关系的派生展示字段，不是数据库中的状态真源。

正文顺序：

1. 摘要；
2. 要点；
3. 原始正文；
4. 融合后的完整字幕或语音稿；
5. 图片及其就地 OCR；
6. 本地附件；
7. 来源信息；
8. 用户笔记区。

自动生成内容放在受控标记中：

```markdown
<!-- smfa:generated:start -->
自动生成内容
<!-- smfa:generated:end -->

## 我的笔记

这里的内容由用户维护，重新同步时不得覆盖。
```

图文笔记中每张图片后紧跟该图片的 OCR 文本，不把所有图片和所有 OCR 分别堆放。视频文字段始终保留时间戳；只有平台确认支持稳定时间参数时才生成来源深链，首版优先支持 Bilibili，不能为小红书或抖音伪造不可靠的时间跳转 URL。

作者、收藏夹和标签生成可导航关系及确定性索引页。frontmatter 保持便于查询的纯值，正文关系区生成 `[[双链]]`；跨条目主题 MOC 属于大模型增强阶段，不作为 0.1 阻塞条件。

文件名采用“可读标题 + 短内容 ID”，但 `smfa_id` 才是笔记身份。每次同步开始时扫描配置知识库中的 Markdown frontmatter，建立 `smfa_id -> 当前路径` 索引，因此用户手动移动或重命名笔记不会生成重复文件。数据库路径只是可重建缓存。

渲染器必须遵循冲突保护：

- 找不到任一生成区标记或标记顺序异常时，将条目标记为 `note_conflict` 并跳过自动写入，不能追加第二个生成区。
- 更新 frontmatter 时保留未知字段。标签合并以旧的 `smfa_generated_tags` 区分用户标签与旧生成标签，再将用户标签和新生成标签合并到 `tags`。
- 写入先落到同目录临时文件，重新解析 frontmatter、生成区和用户笔记区后原子替换。
- 骨架笔记与完整笔记共用同一 `smfa_id` 和路径，只更新 `processing_status` 与生成区。

## 13. CLI 与 Agent 工作流

建议提供以下命令：

```text
smfa init                      # 初始化配置和知识库
smfa doctor                    # 检查 Chrome、FFmpeg、模型和目录权限
smfa login <platform>          # 建立或修复平台登录状态
smfa sync [platform|all]       # 增量同步收藏
smfa sync [platform|all] --full # 禁用 early-stop，完整枚举并允许 reconciliation
smfa sync [platform|all] --metadata-only # 只生成骨架并入队重任务
smfa worker                    # 消费持久化重处理队列
smfa status                    # 查看平台、队列和最近运行结果
smfa retry [job-id|failed]     # 重试失败任务
smfa item <canonical-id>       # 查看单条处理状态
smfa export-report             # 导出脱敏运行报告
smfa cleanup --dry-run         # 预览可清理临时文件
```

Agent 调用时遵循：

1. 首次执行 `doctor`，解释缺失依赖。
2. 检查三个平台会话；只有需要时才执行登录流程。
3. 默认先完成轻量枚举和骨架渲染，再在当前进程继续消费重处理队列；安装用户级 worker 后可异步消费。
4. 遇到验证码或账号确认时暂停对应平台，明确告诉用户需要完成的动作。
5. 同步报告区分“已发现/骨架可见”和“重处理完成”，并列出新增、更新、跳过、失败、待验证和已清理数量。
6. 未经用户显式要求，不启用定时任务，也不改变知识库位置。

完成手动运行验证后，可以提供 `smfa schedule install --interval 6h`，在 macOS 使用 LaunchAgent，在 Linux 使用 systemd user timer。定时运行只复用已有登录状态，不能静默要求用户输入凭证。

## 14. 配置与秘密管理

配置优先级：命令行参数 > 环境变量 > 用户配置文件 > 默认值。

用户配置文件位于操作系统应用配置目录，不进入仓库。配置内容包括：

- 知识库路径；
- 启用的平台和收藏夹过滤规则；
- ASR、OCR 和摘要开关；
- 模型名称、设备和资源限制；
- ASR 热词、转写后领域词典和 OCR 术语纠错词典；
- early-stop 连续已知条目阈值、重叠窗口和完整同步周期；
- 并发数、请求间隔和重试上限；
- 原始视频清理策略；
- Markdown 模板与语言。

API Key 优先存放在系统 Keychain/Secret Service；环境变量作为后备。日志必须对 Authorization、Cookie、Token、API Key、手机号、邮箱和本地用户名执行脱敏。

## 15. 可靠性与错误处理

### 15.1 分类

- `needs_auth`：登录过期，暂停该平台并提示重新登录。
- `needs_user_action`：验证码、设备确认或平台明确要求的人工步骤。
- `rate_limited`：按平台退避并降低并发，其他平台继续。
- `layout_changed`：关键字段缺失，停止该适配器并生成脱敏诊断。
- `enumeration_incomplete`：收藏夹未到达结束游标，禁止执行取消收藏 reconciliation。
- `media_unavailable`：内容删除、地区限制或仅好友可见；保留元数据笔记。
- `processor_failed`：ASR/OCR/摘要失败；保留临时材料并支持阶段重试。
- `note_conflict`：Markdown 身份、frontmatter 或生成区标记异常；保留现有文件并等待用户处理。
- `disk_pressure`：空间不足时停止新下载，不删除未经验证的用户文件。

### 15.2 重试

- 网络和限流错误采用带抖动的指数退避。
- 页面结构错误、认证错误和权限错误不盲目重试。
- 每个阶段有独立尝试次数，避免从收藏列表重新开始整条流水线。
- 进程异常退出后，从 SQLite 中最后一个持久化状态恢复。

### 15.3 运行报告

每次同步输出平台级统计和失败摘要。日志采用结构化格式，并为用户提供默认简洁视图。诊断信息不包含完整页面、Cookie 或无关账号数据。

## 16. 安全、隐私与合规

- 仅处理用户本人有权访问并主动收藏的内容。
- 默认低并发并配置随机化请求间隔，避免给平台造成异常负载。
- 不实现验证码破解、签名绕过、反检测补丁或访问权限规避。
- 浏览器会话、缓存、数据库和模型文件均在用户本地。
- 第三方大模型调用默认仅上传文字，并在初始化时明确说明数据边界。
- `cleanup` 必须进行真实路径解析、根目录验证、数据库归属验证和 dry-run 支持。
- 永不对用户的知识库根目录、Home 目录或未登记路径执行递归删除。
- 平台适配器文档记录已知限制和维护日期，发布时附带“平台变化可能导致暂时不可用”的说明。
- 项目采用清晰的开源许可证；依赖的模型、下载器和 SDK 许可证需单独核验并记录。

## 17. 测试与验收

### 17.1 单元测试

- 标准化数据模型和字段校验。
- 状态机的合法与非法迁移。
- 收藏夹成员关系、条目派生状态，以及“从 A 移除但仍在 B”的场景。
- 元数据指纹、素材哈希、提取缓存键和素材去重。
- Markdown 骨架/完整渲染、`smfa_id` 路径重建、标记冲突和用户笔记区保留。
- frontmatter 未知字段、用户标签和生成标签的合并。
- 路径验证、清理白名单和脱敏逻辑。
- 全派生产物清理屏障；任一子任务失败时原视频不得删除。
- LLM 结构化响应校验及降级行为。

### 17.2 适配器契约测试

- 使用脱敏的收藏分页、详情和媒体响应 fixture。
- 验证字段缺失时明确失败，而不是生成空条目。
- 验证分页、重复条目、取消收藏和不可用内容。
- 验证登录失效、限流和布局变化导致的中断枚举不会触发错误 reconciliation。
- 验证 early-stop 重叠窗口、旧条目重新收藏和定期完整同步。
- 每个平台适配器可以独立运行测试。

### 17.3 集成测试

- 使用短视频、无字幕视频、多图笔记、纯文字笔记和重复收藏样本。
- 验证轻量枚举先生成全部骨架，重处理队列随后就地补全同一批笔记。
- 启动两个 worker 竞争同一任务，验证 SQLite 租约和条目级文件锁不会产生重复处理或并发覆盖。
- 验证 FFmpeg、ASR、OCR、时间轴融合、Markdown 和清理流程完整连接。
- 使用口播＋满屏字幕、静止画面换字幕、纯视觉标注和无口播教程样本验证融合去重。
- 在 Apple Silicon 上执行 RapidOCR 与 `mlx-whisper`/`whisper.cpp` 冒烟和性能基准；Linux/NVIDIA 后端单独验证。
- 模拟中途退出，确认重启后从持久化阶段继续。
- 模拟大模型不可用，确认基础归档仍成功。
- 模拟磁盘空间不足，确认不会删除未验证文件。

### 17.4 端到端验收

以下是 1.0 三平台稳定版的共同验收标准；各阶段只对已声明为 stable 的平台承诺相同质量门：

本轮发布执行采用用户明确指定的轻量真实内容范围：每个平台只选择一个真实代表条目贯通下载、提取、融合、渲染、验证和清理；其余内容类型通过真实元数据盘点与脱敏 fixture/契约测试覆盖。该范围足以验证三平台工作流接线和安全边界，但验证报告必须逐项列出未做真实重处理的类型，且不得把这些类型描述为“已通过真实端到端”。完整集合可因个人收藏规模使用有记录的限量复跑验证幂等性，不因此把未观察部分标记为移除。

1. 三个平台各能通过用户登录态读取至少一个收藏夹并增量发现内容。
2. 轻量枚举结束后全部收藏先出现为可浏览的 Markdown 骨架，重处理结果随后就地补全，不生成重复笔记。
3. 视频有可用字幕时不执行 ASR；无字幕时生成带时间戳的本地文字稿。小红书和抖音的容量测试按多数视频执行 ASR。
4. 小红书或抖音多图内容保存有序原图，并在每张图片下方生成对应 OCR。
5. 口播与烧录字幕融合后不重复输出同一句话，纯视觉标注仍被保留。
6. 相同收藏重复同步不会生成重复 Markdown 或重复素材。
7. 用户移动/重命名笔记后仍通过 `smfa_id` 定位；标记损坏时报告冲突而不覆盖。
8. 用户笔记、未知 frontmatter 字段和用户标签在再次同步后保持不变。
9. 未完整枚举的收藏夹不会产生取消收藏误标；一个条目仍在其他收藏夹时不会显示为全部取消。
10. 大模型关闭或失败时仍能完成本地归档。
11. 只有所有派生产物经过验证的临时视频会被删除；故障注入测试中不得误删知识库或用户文件。
12. 登录失效或验证码出现时，对应平台安全暂停，其他平台可以继续。
13. Obsidian 能直接打开知识库，图片链接、索引和内部路径有效。
14. 中英文收藏归档需求能触发 Skill，单链接转写、内容发布、营销文案等无关请求不应触发。

0.1 stable 的额外发布边界：

- Bilibili 满足上述所有适用质量门。
- 小红书和抖音可以随代码提供实验性适配器，但不得在 Skill metadata 或 README 中宣称 stable。
- 0.1 的核心数据模型、两段式同步、清理屏障和笔记保护必须按三平台需求设计，不能写成 Bilibili 专用实现。

## 18. Skill 触发评测

正向测试至少包括：

- “帮我把 B 站收藏夹里的视频转成文字，整理到 Obsidian。”
- “同步我的小红书收藏，把图和图片里的文字都保留下来。”
- “Back up my saved Douyin videos and turn them into searchable Markdown notes.”
- “我想把几个短视频平台的收藏做成本地知识库。”

负向测试至少包括：

- “帮我写一篇小红书营销文案。”
- “分析这个公开 B 站视频的数据表现。”
- “给我的抖音视频加字幕并重新发布。”
- “帮我规划社交媒体内容日历。”
- “把这个单独的 B 站视频链接转成文字并总结。”
- “提取这张图片里的文字。”

评测不仅检查 Skill 是否被选中，还检查 Agent 是否遵守本地处理、登录授权、禁止绕过验证和安全清理边界。

## 19. 分阶段实施

### 阶段 0：仓库与规范

- 完成设计审查，并添加 MIT 许可证、贡献指南和安全说明。
- 固化 Skill metadata、配置 schema、标准化模型和 CLI 契约。

### 阶段 1：0.1 stable——核心流水线 + Bilibili

- 实现配置、SQLite 薄封装、两段式同步、Markdown 骨架/完整渲染和安全缓存。
- 实现成员关系状态、完整枚举 reconciliation、指纹分层和全派生产物清理屏障。
- 直接封装 `yt-dlp` 完成 Bilibili 收藏枚举、字幕优先、ASR 后备和封面归档。
- 建立第一套端到端测试和故障恢复测试。

### 阶段 2：0.2 experimental——小红书

- 实现收藏枚举、图文正文、多图素材和视频处理。
- 完成原图优先、逐图 OCR、ASR 主路径、素材质量标记和适配器诊断。

### 阶段 3：0.3 experimental——抖音与文本融合

- 实现收藏枚举、视频/图集分流、字幕探测、ASR 主路径和自适应画面 OCR。
- 完成 ASR × OCR 时间轴融合和烧录字幕去重。
- 完成三个平台统一增量同步报告。

### 阶段 4：大模型增强与自动运行

- 增加可选摘要、标签和主题提取。
- 增加 LaunchAgent/systemd user timer。
- 完成资源限制、磁盘压力处理和长时间稳定性测试。

### 阶段 5：1.0 stable——三平台 Skill 发布

- 完成中英文 README、安装脚本、示例知识库和故障排查文档。
- 运行 Skill 触发评测和安全审计。
- 发布到 GitHub，并准备 Skills.sh 与 ClawHub 可发现的安装入口。

各阶段都必须保持主分支可运行。0.1 只把 Bilibili 标记为 stable；小红书和抖音在通过相同质量门前保持 experimental。1.0 才承诺三个平台全部稳定。项目名称和总体目标不缩水，但 Skill metadata 必须如实反映当前发布版本的支持状态。

## 20. 主要风险与缓解措施

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 平台页面或私有响应变化 | 采集暂停 | 独立适配器、契约 fixture、关键字段校验、脱敏诊断 |
| 登录过期或风控 | 自动同步中断 | 持久会话、平台级暂停、人工验证后断点续传 |
| 中断枚举触发错误取消收藏 | 收藏关系被误标 | 每收藏夹完整枚举门、run completion 标记、失败时禁止 reconciliation |
| early-stop 漏掉重排或重新收藏条目 | 新内容未发现 | 连续阈值、重叠窗口、异常禁用、定期强制完整同步 |
| ASR/OCR 本地资源消耗 | 速度慢、占用磁盘 | 任务队列、模型按需加载、关键帧去重、资源上限 |
| Apple Silicon 后端不可用或过慢 | macOS 首版无法运行 | RapidOCR/ONNX 默认、MLX/whisper.cpp 后备、目标硬件基准测试 |
| ASR 与烧录字幕重复或错误融合 | 文字稿重复、信息丢失 | 时间窗与相似度规则、来源保留、冲突不删除、融合 fixture |
| 原始视频清理误删 | 严重数据损失 | 专属缓存根、数据库归属、真实路径校验、验证门、dry-run |
| Markdown 重写用户内容 | 用户笔记丢失 | `smfa_id` 身份、生成区冲突保护、frontmatter 合并、原子写入 |
| 云端摘要泄露内容 | 隐私风险 | 默认仅传文字、明确开关、字段白名单、日志脱敏 |
| Skill 搜索命中不足 | 采用率低 | 明确名称、双语描述、平台/动作/输出关键词、触发评测 |
| Skill 误触发 | Agent 执行无关流程 | 负向评测、描述限定为收藏/保存内容归档场景 |

## 21. 成功指标

- 用户完成一次登录后，可以通过一条 Agent 指令先看到全部收藏骨架，并让重处理队列继续补全。
- 已处理且未变化的条目再次同步时不发生昂贵的下载、ASR 或 OCR。
- 单条内容失败不会导致整个跨平台同步丢失进度。
- 不完整枚举不会错误改变收藏关系；跨收藏夹条目的状态可正确派生。
- 所有生成笔记均可追溯到平台来源和本地素材。
- 短视频口播和烧录字幕形成无明显重复的融合文字稿，同时保留视觉补充信息。
- 无云端大模型时仍具备完整的采集、转写、OCR 和 Markdown 归档能力。
- 清理测试在任何故障路径上都不会删除知识库或未登记文件。
- Skill 在中英文正向测试中稳定触发，在社交媒体创作类负向测试中不触发。

## 22. 外部审查处理记录

第一轮外部审查意见按以下原则进入本设计：

- **直接采纳**：两段式同步、ASR × OCR 融合、Bilibili 封装 `yt-dlp`、收藏夹关系状态、完整枚举 reconciliation、指纹分层、全派生产物清理屏障、`smfa_id` 笔记定位、frontmatter 合并和触发范围收紧。
- **调整后采纳**：小红书/抖音按 ASR 高频路径规划，但仍保留廉价字幕探测；early-stop 增加重叠窗口和完整同步；macOS 使用 RapidOCR 与 Apple 原生 Whisper 后备；私有接口走浏览器上下文，但静态素材仍可使用受限 HTTP 客户端；Obsidian MOC 后置。
- **明确不采纳**：完全删除定时采样。烧录字幕可在静止场景中变化，因此仍需低频采样、文字区域变化检测与感知哈希共同工作。
- **发布策略调整**：0.1 只承诺 Bilibili stable，小红书和抖音逐步以 experimental 交付，1.0 才承诺三平台稳定；项目最终目标保持不变。

下一轮审查应重点验证：

1. 两段式状态机和 worker 边界是否足够明确，是否会产生并发 Markdown 写入。
2. early-stop 与完整 reconciliation 是否在所有中断路径上互斥。
3. 融合器能否处理快速换字幕、无口播和纯视觉标注。
4. `smfa_id` 扫描、frontmatter 合并和标记冲突策略是否覆盖 Obsidian 常见编辑行为。
5. Apple Silicon 默认后端是否能在目标硬件上通过准确率、速度和内存基准。
6. 各发布阶段的 Skill metadata 是否会准确表达 stable/experimental 支持范围。

## 23. 已核验的技术依据

以下链接用于记录本轮外部审查中会影响架构决策的上游现状；实现时仍需锁定版本并通过真实账号 fixture 复验：

- [yt-dlp Bilibili extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py)：包含 Bilibili 收藏夹、WBI、登录字幕和视频提取逻辑。
- [yt-dlp 小红书 extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/xiaohongshu.py)：当前实现提取视频、`imageList` 图片和正文元数据，没有通用字幕输出。
- [yt-dlp 抖音/TikTok extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/tiktok.py)：当前代码存在 `auto_captions`、`caption_infos` 和 `subtitleInfos` 探测路径，因此不能断言抖音永远没有机器字幕。
- [PaddleOCR Apple Silicon Issue #18057](https://github.com/PaddlePaddle/PaddleOCR/issues/18057)：确认 ARM64 中文识别异常，同时维护者给出关闭 MKL-DNN 的规避方式；本项目仍选择更轻量的 RapidOCR 作为 macOS 默认。
- [RapidOCR](https://github.com/RapidAI/RapidOCR)：使用 ONNX 模型并声明支持 macOS，首版仍需在目标 Apple Silicon 机器上实测。
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper#gpu)：GPU 路径要求 NVIDIA CUDA，不作为 Apple Silicon 默认后备。
- [MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) 与 [whisper.cpp](https://github.com/ggml-org/whisper.cpp#core-ml-support)：作为 Apple Silicon 的 Whisper 后备候选，以实际基准结果决定默认值。
- [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)：验证了真实浏览器、CDP 和页面 JS 上下文在小红书/抖音采集中的可行性，但不构成“所有请求只能靠 XHR 拦截”的依据。
