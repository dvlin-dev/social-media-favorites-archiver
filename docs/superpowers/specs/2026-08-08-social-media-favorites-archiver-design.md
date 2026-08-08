# Social Media Favorites Archiver 设计方案

- 项目名称：`social-media-favorites-archiver`
- 展示名称：Social Media Favorites Archiver｜社交媒体收藏归档
- 设计日期：2026-08-08
- 状态：设计完成，等待外部 Agent 审查
- 首要平台：Bilibili、小红书（Xiaohongshu/RedNote）、抖音（Douyin）
- 计划许可证：MIT

## 1. 摘要

本项目提供一个面向 Agent 的开源 Skill，把用户在 Bilibili、小红书和抖音中的个人收藏自动同步到本地，并将视频、图文笔记及其元数据整理成可由 Obsidian 直接使用的 Markdown 知识库。

系统复用用户已经授权的浏览器登录状态，按平台增量读取收藏内容。视频优先提取原生字幕；无字幕时在本地执行语音识别。图片和视频关键帧在本地执行 OCR。原始正文、字幕、OCR 结果和媒体附件保存在本地；只有提取后的文字可以发送给用户配置的大模型，用于生成摘要、标签和主题。原始视频只作为临时处理材料，归档校验成功后删除。

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

Skill 目录名使用明确的任务词，而不是抽象品牌名：

```yaml
---
name: social-media-favorites-archiver
description: >
  Archive, transcribe, OCR, summarize, and organize saved or favorited
  social-media content from supported platforms—currently Bilibili,
  Xiaohongshu/RedNote, and Douyin—into a local Markdown or Obsidian
  knowledge base.
  Use whenever the user asks to sync, download, back up, migrate, transcribe,
  extract text from, summarize, or organize social-media favorites,
  collections, bookmarks, saved videos, image posts, or notes.
  也适用于用户提到整理收藏、收藏夹备份、视频转文字、提取图文笔记、
  保存小红书/B站/抖音内容、建立本地知识库或导入 Obsidian 的场景，
  即使用户没有明确使用“归档”或“Skill”这个词。
---
```

触发设计遵循以下原则：

- 名称表达核心对象和动作：social media、favorites、archive。
- 描述同时覆盖平台名、任务动词、内容类型和输出目标。
- 同时包含中文与英文常见说法，兼顾语义检索和关键词检索。
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
临时媒体处理流水线
    ├── 原生字幕/正文提取
    ├── 本地 ASR
    ├── 图片与关键帧 OCR
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
3. **核心编排层**：负责队列、幂等、重试、状态迁移、配置和运行报告。
4. **内容处理层**：负责字幕、ASR、OCR、摘要和结构化内容生成。
5. **存储与输出层**：负责 SQLite、Markdown、附件、日志和安全清理。

每一层通过明确的数据对象交互。平台适配器不得直接写 Markdown；处理器不得知道平台页面结构；渲染器不得负责下载媒体。

## 6. 建议技术栈

- 运行时：Python 3.11 及以上。
- 包与环境管理：`uv`，同时保持标准 `pip` 安装兼容性。
- CLI：Typer。
- 数据校验：Pydantic。
- 本地数据库：SQLite，启用 WAL；数据库访问采用 SQLAlchemy 或轻量等价实现。
- HTTP：`httpx`，仅用于允许直接请求且登录信息可安全继承的接口。
- 浏览器：Chrome/Chromium 登录配置文件；自动化实现优先采用 Playwright 并通过 CDP 连接受控浏览器。
- 媒体下载：平台适配逻辑与 `yt-dlp` 组合，不能由 `yt-dlp` 处理的素材由浏览器会话下载。
- 音视频处理：FFmpeg。
- 中文语音识别：本地 FunASR 兼容模型为默认；Whisper 兼容实现作为多语言和失败后备。
- OCR：PaddleOCR；视频先用 FFmpeg 做场景检测和关键帧抽取。
- 摘要接口：OpenAI-compatible 文本接口，提供完全关闭选项。
- 测试：pytest、录制并脱敏的平台 fixture、少量受控端到端账号测试。

首版优先支持 macOS，其次支持 Linux。Windows 支持不作为 0.1 发布阻塞条件。

## 7. 仓库结构

```text
social-media-favorites-archiver/
├── SKILL.md
├── README.md
├── LICENSE
├── pyproject.toml
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
│   │   └── enrichment.py
│   ├── storage/
│   │   ├── database.py
│   │   ├── assets.py
│   │   └── markdown.py
│   └── safety/
│       ├── cleanup.py
│       ├── redaction.py
│       └── paths.py
├── references/
│   ├── configuration.md
│   ├── platform-bilibili.md
│   ├── platform-xiaohongshu.md
│   ├── platform-douyin.md
│   └── troubleshooting.md
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   └── fixtures/
├── evals/
│   └── evals.json
└── docs/
    └── superpowers/specs/
```

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

- 通过已登录页面或可验证的登录态接口枚举收藏夹及其分页。
- 优先获取平台字幕、视频元数据、UP 主、发布日期、收藏夹名称、封面和 BV/AV 标识。
- 无原生字幕时才下载足够用于转写的媒体。
- 下载实现可以复用经过验证的 `yt-dlp` 能力，但收藏枚举、状态记录和输出仍由本项目负责。
- 分 P 视频的每一 P 作为子章节处理，共享一个主收藏条目。

### 8.4 小红书

- 通过用户已登录的收藏页面枚举笔记；优先读取页面可见数据和浏览器已加载的结构化响应。
- 支持纯文字、多图、图文和视频笔记。
- 保存正文、作者、发布时间、收藏时间（可取得时）、标签、原图、视频封面和原始链接。
- 图片按展示顺序保存，OCR 结果与对应图片建立明确关联。
- 如果页面仅暴露缩略图，适配器应记录素材质量，不得把低清图伪装为原图。

### 8.5 抖音

- 通过用户已登录的收藏页面枚举作品和收藏夹。
- 保存作品文案、作者、发布时间、封面、原始链接和平台内容 ID。
- 优先提取平台可用字幕或文字轨；无字幕时下载临时视频并执行本地 ASR。
- 图集作品按图文内容处理，不强制进入视频流水线。

### 8.6 平台变化处理

采集采用“结构化响应优先、稳定 DOM 后备”的策略。每个适配器维护：

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
  favorite_state     # active | removed | unavailable
  collections[]
  original_text
  native_subtitles[]
  assets[]
  content_hash
  platform_metadata
```

SQLite 至少包含以下逻辑表：

- `items`：标准化内容、当前状态和内容指纹。
- `collections`：平台收藏夹及其本地映射。
- `item_collections`：内容与收藏夹的多对多关系。
- `assets`：素材来源、类型、哈希、本地路径和清理状态。
- `jobs`：处理阶段、尝试次数、下一次重试时间和最后错误。
- `runs`：每次同步的开始、结束、平台统计和最终结果。
- `enrichments`：摘要模型、提示词版本、结构化结果和文字输入哈希。

平台原始元数据只保存必要字段；大型响应和含敏感信息的完整响应不进入长期数据库。

## 10. 增量同步与任务状态机

每个条目按以下状态前进：

```text
discovered
  -> metadata_ready
  -> assets_ready
  -> extracted
  -> enriched
  -> rendered
  -> verified
  -> cleaned
```

失败不会回滚已经验证的阶段，而是记录在当前阶段并进入 `retryable`、`needs_auth`、`needs_user_action` 或 `permanent_failure` 子状态。

幂等规则：

- `canonical_id` 决定条目身份。
- `content_hash` 未变化时跳过正文、转写、OCR 和渲染。
- 素材以 SHA-256 去重；相同图片只保存一次或建立复用引用。
- 摘要以“文字输入哈希 + 模型 + 提示词版本”作为缓存键。
- Markdown 使用稳定路径；重复同步更新自动生成区域，不创建重复文件。
- 同一平台同时只允许一个收藏枚举任务，防止游标和限流相互干扰。

收藏取消处理：完整同步结束后，将本次未出现但此前存在的条目标记为 `removed`。默认保留 Markdown 和附件，不执行删除。

## 11. 内容处理流水线

### 11.1 临时下载

- 临时文件存放在应用专属缓存目录，不放入 Obsidian 知识库。
- 下载采用 `.partial` 临时名，完成并校验大小后原子重命名。
- 每个文件记录来源、预期用途、哈希和归属条目。
- 清理器只能删除数据库中登记且位于已验证缓存根目录下的文件。

### 11.2 字幕和 ASR

处理优先级：

1. 平台原生字幕；
2. 媒体中已有的字幕轨；
3. 本地语音识别。

本地 ASR 前由 FFmpeg 提取单声道、16 kHz 音频。中文内容默认使用 FunASR 兼容模型；检测到多语言、主要模型失败或用户显式配置时使用 Whisper 兼容后备。输出保留分段时间戳，并生成可阅读的连续文本。首版不要求说话人分离。

只有在字幕或 ASR 结果通过以下检查后，系统才允许进入视频清理阶段：

- 输出非空；
- 音频时长与处理记录一致；
- 分段时间戳单调且未超过媒体时长；
- 文字稿已写入数据库并渲染到 Markdown；
- Markdown 文件重新读取校验成功。

### 11.3 OCR

- 小红书与抖音图集的每张原图执行 PaddleOCR。
- 视频使用场景切换、时间间隔和图像感知哈希共同筛选关键帧。
- 对高度相似的关键帧去重，避免重复 OCR 和大量无意义截图。
- OCR 结果按素材文件分组，保留顺序和可追溯引用。
- 低置信度文本保留但标注置信度，不与作者正文混合成不可区分的内容。

### 11.4 大模型增强

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
content_hash: "sha256:..."
---
```

正文顺序：

1. 摘要；
2. 要点；
3. 原始正文；
4. 完整字幕或语音稿；
5. 图片 OCR；
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

文件名采用“可读标题 + 短内容 ID”，标题变更时默认保持既有路径，避免破坏 Obsidian 链接。非法字符统一替换，冲突由短 ID 消除。

## 13. CLI 与 Agent 工作流

建议提供以下命令：

```text
smfa init                      # 初始化配置和知识库
smfa doctor                    # 检查 Chrome、FFmpeg、模型和目录权限
smfa login <platform>          # 建立或修复平台登录状态
smfa sync [platform|all]       # 增量同步收藏
smfa status                    # 查看平台、队列和最近运行结果
smfa retry [job-id|failed]     # 重试失败任务
smfa item <canonical-id>       # 查看单条处理状态
smfa export-report             # 导出脱敏运行报告
smfa cleanup --dry-run         # 预览可清理临时文件
```

Agent 调用时遵循：

1. 首次执行 `doctor`，解释缺失依赖。
2. 检查三个平台会话；只有需要时才执行登录流程。
3. 默认执行增量同步，不执行全量重新处理。
4. 遇到验证码或账号确认时暂停对应平台，明确告诉用户需要完成的动作。
5. 同步结束后报告新增、更新、跳过、失败、待验证和已清理数量。
6. 未经用户显式要求，不启用定时任务，也不改变知识库位置。

完成手动运行验证后，可以提供 `smfa schedule install --interval 6h`，在 macOS 使用 LaunchAgent，在 Linux 使用 systemd user timer。定时运行只复用已有登录状态，不能静默要求用户输入凭证。

## 14. 配置与秘密管理

配置优先级：命令行参数 > 环境变量 > 用户配置文件 > 默认值。

用户配置文件位于操作系统应用配置目录，不进入仓库。配置内容包括：

- 知识库路径；
- 启用的平台和收藏夹过滤规则；
- ASR、OCR 和摘要开关；
- 模型名称、设备和资源限制；
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
- `media_unavailable`：内容删除、地区限制或仅好友可见；保留元数据笔记。
- `processor_failed`：ASR/OCR/摘要失败；保留临时材料并支持阶段重试。
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
- 内容哈希、素材去重和稳定文件名。
- Markdown 渲染及用户笔记区保留。
- 路径验证、清理白名单和脱敏逻辑。
- LLM 结构化响应校验及降级行为。

### 17.2 适配器契约测试

- 使用脱敏的收藏分页、详情和媒体响应 fixture。
- 验证字段缺失时明确失败，而不是生成空条目。
- 验证分页、重复条目、取消收藏和不可用内容。
- 每个平台适配器可以独立运行测试。

### 17.3 集成测试

- 使用短视频、无字幕视频、多图笔记、纯文字笔记和重复收藏样本。
- 验证 FFmpeg、ASR、OCR、Markdown 和清理流程完整连接。
- 模拟中途退出，确认重启后从持久化阶段继续。
- 模拟大模型不可用，确认基础归档仍成功。
- 模拟磁盘空间不足，确认不会删除未验证文件。

### 17.4 端到端验收

0.1 版本完成时必须满足：

1. 三个平台各能通过用户登录态读取至少一个收藏夹并增量发现内容。
2. 视频有原生字幕时不执行 ASR；无字幕时生成带时间戳的本地文字稿。
3. 小红书或抖音多图内容保存有序图片并生成逐图 OCR。
4. 相同收藏重复同步不会生成重复 Markdown 或重复素材。
5. 修改自动生成区后可更新，用户笔记区在再次同步后保持不变。
6. 大模型关闭或失败时仍能完成本地归档。
7. 只有经过验证的临时视频会被删除；故障注入测试中不得误删知识库或用户文件。
8. 登录失效或验证码出现时，对应平台安全暂停，其他平台可以继续。
9. Obsidian 能直接打开知识库，图片链接和内部路径有效。
10. 中英文典型需求能触发 Skill，内容发布、营销文案等无关请求不应触发。

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

评测不仅检查 Skill 是否被选中，还检查 Agent 是否遵守本地处理、登录授权、禁止绕过验证和安全清理边界。

## 19. 分阶段实施

### 阶段 0：仓库与规范

- 完成设计审查，并添加 MIT 许可证、贡献指南和安全说明。
- 固化 Skill metadata、配置 schema、标准化模型和 CLI 契约。

### 阶段 1：核心流水线 + Bilibili

- 实现配置、SQLite 状态机、Markdown 渲染和安全缓存。
- 完成 Bilibili 收藏枚举、字幕优先、ASR 后备和封面归档。
- 建立第一套端到端测试和故障恢复测试。

### 阶段 2：小红书

- 实现收藏枚举、图文正文、多图素材和视频处理。
- 完成逐图 OCR、素材质量标记和适配器诊断。

### 阶段 3：抖音

- 实现收藏枚举、视频/图集分流、字幕/ASR 和封面处理。
- 完成三个平台统一增量同步报告。

### 阶段 4：大模型增强与自动运行

- 增加可选摘要、标签和主题提取。
- 增加 LaunchAgent/systemd user timer。
- 完成资源限制、磁盘压力处理和长时间稳定性测试。

### 阶段 5：Skill 发布

- 完成中英文 README、安装脚本、示例知识库和故障排查文档。
- 运行 Skill 触发评测和安全审计。
- 发布到 GitHub，并准备 Skills.sh 与 ClawHub 可发现的安装入口。

各阶段都必须保持主分支可运行。Bilibili 端到端链路先验证核心架构，但 0.1 正式发布需要三个平台全部达到端到端验收标准。

## 20. 主要风险与缓解措施

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 平台页面或私有响应变化 | 采集暂停 | 独立适配器、契约 fixture、关键字段校验、脱敏诊断 |
| 登录过期或风控 | 自动同步中断 | 持久会话、平台级暂停、人工验证后断点续传 |
| ASR/OCR 本地资源消耗 | 速度慢、占用磁盘 | 任务队列、模型按需加载、关键帧去重、资源上限 |
| 原始视频清理误删 | 严重数据损失 | 专属缓存根、数据库归属、真实路径校验、验证门、dry-run |
| Markdown 重写用户内容 | 用户笔记丢失 | 生成区标记、独立用户笔记区、渲染回归测试 |
| 云端摘要泄露内容 | 隐私风险 | 默认仅传文字、明确开关、字段白名单、日志脱敏 |
| Skill 搜索命中不足 | 采用率低 | 明确名称、双语描述、平台/动作/输出关键词、触发评测 |
| Skill 误触发 | Agent 执行无关流程 | 负向评测、描述限定为收藏/保存内容归档场景 |

## 21. 成功指标

- 用户完成一次登录后，可以通过一条 Agent 指令增量同步三个平台。
- 已处理且未变化的条目再次同步时不发生昂贵的下载、ASR 或 OCR。
- 单条内容失败不会导致整个跨平台同步丢失进度。
- 所有生成笔记均可追溯到平台来源和本地素材。
- 无云端大模型时仍具备完整的采集、转写、OCR 和 Markdown 归档能力。
- 清理测试在任何故障路径上都不会删除知识库或未登记文件。
- Skill 在中英文正向测试中稳定触发，在社交媒体创作类负向测试中不触发。

## 22. 审查重点

外部审查应重点挑战以下问题：

1. 登录态自动采集是否有更稳定且更少依赖页面结构的实现边界？
2. 平台适配器接口能否覆盖分页、收藏夹和不同内容类型，又不会泄露平台细节？
3. 状态机是否足以保证幂等、断点续传和原始视频安全清理？
4. Markdown 生成区策略是否能可靠保护用户编辑？
5. 本地处理与云端摘要的数据边界是否明确、可测试？
6. 首版范围是否仍然过大，阶段顺序是否合理？
7. Skill 名称和双语描述能否同时提高召回率并避免误触发？
8. 是否存在遗漏的安全、许可证、平台合规或测试风险？
