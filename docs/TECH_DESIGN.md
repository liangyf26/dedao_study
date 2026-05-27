# 得到内容同步到 Obsidian - 技术设计文档 v0.1

最后更新：2026-05-26

## 1. 设计目标

构建一个本地 Python CLI 项目，自动登录得到网页版、检查固定栏目更新、提取网页文字稿、生成 Zettelkasten 风格 Markdown 笔记、写入 Obsidian，并在每日执行后推送飞书通知。

MVP 优先解决“网页文字稿同步”。转录模块先设计接口，待选型确认后实现。

## 2. 总体架构

```text
CLI
  |
  +-- ConfigLoader
  +-- BrowserSession
  +-- DedaoCrawler
  +-- TranscriptExtractor
  +-- SummaryService
  +-- MarkdownWriter
  +-- SyncRepository(SQLite)
  +-- Notifier(Feishu)
  +-- TranscriptionService(phase 2)
```

核心流程：

```text
load config
  -> load login state
  -> iterate columns
  -> parse item list
  -> filter synced items
  -> fetch detail page
  -> extract transcript
  -> summarize if enabled
  -> write markdown
  -> update sqlite
  -> send feishu report
```

## 3. 推荐目录结构

```text
603_dedao_study/
  docs/
    PRD.md
    TECH_DESIGN.md
  dedao_sync/
    __init__.py
    cli.py
    config.py
    browser.py
    crawler.py
    extractor.py
    summarizer.py
    markdown.py
    notifier.py
    repository.py
    transcriber.py
    models.py
  data/
    dedao_sync.sqlite3
  logs/
  .env.example
  config.example.yaml
  pyproject.toml
```

敏感文件：

- `.env`
- `data/auth/`
- `data/*.sqlite3`
- `logs/`
- 临时媒体缓存目录

这些文件应加入 `.gitignore`。

## 4. 配置设计

`config.yaml`：

```yaml
obsidian:
  vault_path: "D:\\biji\\openclaw-vault\\5-收件箱(Inbox)"
  output_dir: "得到"
  filename_pattern: "{column}-{published_date}-{title}.md"

dedao:
  auth_state_path: "data/auth/dedao_state.json"
  browser_profile_dir: "data/browser_profile"
  headless: false
  request_interval_seconds: 2
  save_failure_html: false
  failure_snapshot_dir: "data/page_failures"
  columns:
    - name: "快刀青衣·快刀广播站"
      url: "https://aiquan.dedao.cn/courseList?type=1"
      enabled: true
    - name: "尹烨·健康参考"
      url: "https://www.dedao.cn/course/detail?id=zp9lB3q0breKZq4sDWXYjyWxG64dg2"
      enabled: true
    - name: "马江博·政经参考"
      url: "https://www.dedao.cn/course/detail?id=ZWyMAOLnR4xJ1vqse8X65QaE8YG29k"
      enabled: true
    - name: "脱不花·长谈"
      url: "https://www.dedao.cn/course/detail?id=ElLD8OrepAxVvGMs4kJ2oybGdmBnvM"
      enabled: true

summary:
  enabled: true
  provider: "opencode_go"
  model: "deepseek-v4-pro"
  base_url_env: "OPENCODE_GO_BASE_URL"
  api_key_env: "OPENCODE_GO_API_KEY"

transcription:
  enabled: false
  provider: "faster_whisper"
  delete_media_after_transcription: true
  temp_dir: "data/media_cache"

feishu:
  enabled: true
  webhook_url_env: "FEISHU_WEBHOOK_URL"
  secret_env: "FEISHU_WEBHOOK_SECRET"
  include_titles: true
```

`.env.example`：

```dotenv
OPENCODE_GO_BASE_URL=
OPENCODE_GO_API_KEY=
FEISHU_WEBHOOK_URL=
FEISHU_WEBHOOK_SECRET=
```

## 5. 浏览器自动化选型

### 5.1 候选方案

| 方案 | 优点 | 风险 | 结论 |
| --- | --- | --- | --- |
| Playwright Python + Chromium | 成熟、文档完整、登录态保存可靠、调试方便、Python 集成好 | Chromium 体积大，资源占用高 | MVP 推荐 |
| Obscura + Playwright/Puppeteer CDP | Rust 实现，宣称轻量、CDP 兼容、面向抓取和 AI agent | 项目较新，兼容性和登录态稳定性需要验证；得到这类登录站点的行为未知 | 后续作为实验 backend |

### 5.2 Playwright 方案

Playwright 官方支持保存 cookies、localStorage、IndexedDB 等登录状态，并可在后续 context 中复用。官方也提醒登录态文件包含敏感 cookie，不应提交到仓库。

设计：

- `dedao-sync login` 使用 headful Chromium。
- 用户手动登录。
- 登录成功后保存 `storage_state`，并可选保留 persistent profile。
- 保存后校验 `storage_state` 文件：必须是 JSON object，包含 Playwright 的 `cookies` 和 `origins` 列表，且至少有一项非空。
- `doctor` 和 `preflight` 复用同一校验逻辑，避免空文件、坏 JSON 或 `{}` 被误认为已登录。
- `sync` 命令优先使用已保存的 state。
- 如果检测到登录失效，退出并提示重新运行 `login`。

参考：

- Playwright Authentication 文档：https://playwright.dev/python/docs/auth
- Playwright BrowserContext storage_state 文档：https://playwright.dev/python/docs/api/class-browsercontext

### 5.3 Obscura 方案评估

Obscura 官方仓库描述其为 Rust 编写的 headless browser engine，面向 web scraping 和 AI agent automation，支持 V8 和 Chrome DevTools Protocol，并声称可作为 Puppeteer/Playwright 的 headless Chrome 替代品。README 中还列出轻量、低内存、stealth、tracker blocking 等特性。

适合场景：

- 后续 Debian 常驻运行时希望降低资源占用。
- 大量并发抓取，但本项目目前不需要。
- 希望通过 CDP 保持 Playwright 风格 API。

主要风险：

- 项目较新，完整浏览器兼容性未知。
- 得到网页版登录、媒体播放、风控页面是否兼容未知。
- 轻量/stealth 特性不应被用于绕过风控，本项目只做正常个人登录访问。
- Windows 开发调试体验可能不如 Chromium 直接。

结论：

- MVP 使用 Playwright + Chromium。
- 技术上预留 `BrowserBackend` 接口，后续可实验 Obscura。
- 只有当 Playwright 在 Debian 常驻运行中资源占用明显成为问题时，再评估切换。

参考：

- Obscura GitHub：https://github.com/h4ckf0r0day/obscura

## 6. 页面抓取设计

### 6.1 Crawler 分层

`DedaoCrawler` 负责：

- 打开栏目页。
- 等待主要内容区域加载。
- 提取内容列表。
- 打开详情页。
- 获取页面 HTML、可见文本和必要的网络响应元数据。

`TranscriptExtractor` 负责：

- 从 DOM 中提取正文。
- 清洗无关 UI 文本。
- 保留段落结构。
- 判断是否缺少文字稿。
- 从详情页 HTML 元数据中补全真实标题、作者/讲者和发布时间，优先读取 `og:title`、`author`、`article:published_time`、`time[datetime]`、`h1` 和 `title`。

### 6.2 页面结构适配

由于得到网页版可能存在多个站点形态：

- `www.dedao.cn/course/detail?...`
- `aiquan.dedao.cn/courseList?...`

设计上使用 extractor registry：

```text
extractors/
  www_course_detail
  aiquan_course_list
  fallback_visible_text
```

每个 extractor 输出统一模型：

```python
ContentItem(
    source_url,
    dedao_id,
    column_name,
    title,
    published_at,
    author,
    detail_url,
    content_type,
)
```

详情页输出：

```python
ContentDetail(
    item,
    transcript_text,
    has_transcript,
    media_candidates,
    raw_html_hash,
)
```

同步流程以详情页 `ContentDetail.item` 为准写入 Markdown、SQLite 和飞书新增列表，避免栏目列表标题与详情页真实标题不一致。

## 7. 数据库设计

SQLite 文件：`data/dedao_sync.sqlite3`

### 7.1 items

```sql
CREATE TABLE items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_url TEXT NOT NULL,
  dedao_id TEXT,
  column_name TEXT NOT NULL,
  title TEXT NOT NULL,
  published_at TEXT,
  synced_at TEXT,
  content_hash TEXT,
  status TEXT NOT NULL,
  file_path TEXT,
  has_transcript INTEGER NOT NULL DEFAULT 0,
  transcribed INTEGER NOT NULL DEFAULT 0,
  summary_status TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_items_source_url ON items(source_url);
CREATE INDEX idx_items_dedao_column ON items(dedao_id, column_name);
CREATE INDEX idx_items_content_hash ON items(content_hash);
CREATE INDEX idx_items_column_name ON items(column_name);
CREATE INDEX idx_items_status ON items(status);
```

预写入去重顺序：

- `source_url`
- `canonical_url`
- 同一栏目内的 `dedao_id`
- 拉取详情后的全文 `content_hash`

`dedao_id` 不做全局唯一判断，避免不同栏目或不同站点形态下的同名 ID 导致误跳过。真正重复的正文由 `content_hash` 兜底。

### 7.2 runs

```sql
CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  total_columns INTEGER NOT NULL DEFAULT 0,
  discovered_count INTEGER NOT NULL DEFAULT 0,
  new_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  missing_transcript_count INTEGER NOT NULL DEFAULT 0,
  summary_failed_count INTEGER NOT NULL DEFAULT 0,
  log_path TEXT,
  error_message TEXT
);
```

### 7.3 run_items

```sql
CREATE TABLE run_items (
  run_id INTEGER NOT NULL,
  item_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT,
  PRIMARY KEY (run_id, item_id)
);
```

## 8. 状态机

`items.status`：

- `discovered`
- `synced`
- `missing_transcript`
- `summary_failed`
- `transcription_failed`
- `failed`
- `skipped`

推荐规则：

- 已成功写入 Markdown：`synced`
- 有全文但摘要失败：`summary_failed`，同时保留 `file_path`、`has_transcript=1` 和 `synced_at`，表示全文已保存但摘要仍需重试。
- 无网页文字稿且转录未启用：`missing_transcript`
- 单篇异常：`failed`

`runs.status`：

- `success`：没有失败、无文字稿或摘要失败计数。
- `partial_failed`：至少存在 `failed_count`、`missing_transcript_count` 或 `summary_failed_count`。
- `preflight_failed`：配置、vault、登录态等预检查未通过。
- `login_required`：登录态失效，需要重新登录。
- `locked`：已有同步任务运行中。

## 9. 摘要服务设计

接口：

```python
class SummaryService:
    def summarize(self, detail: ContentDetail) -> SummaryResult:
        ...
```

输入：

- 标题
- 栏目
- 发布时间
- 全文稿

输出：

- 原子卡片
- 永久笔记
- 关联主题
- 行动/观察
- 复习问题
- 关键词

### 9.1 Prompt 要求

- 使用中文。
- 不复制大段原文。
- 以“可复用的知识卡片”为目标。
- 将事实、判断、启发分开。
- 对不确定内容标注“需要回看原文确认”。
- 摘要模型优先输出 JSON，字段为 `atomic_cards`、`permanent_note`、`links`、`actions`、`questions`、`keywords`。
- 程序内部将 JSON 解析为结构化 `SummaryResult`，再渲染为 Obsidian Markdown。
- 为兼容模型偶发偏离，解析器保留 Markdown 章节兜底，支持中文标题、编号标题和代码块包裹的 JSON。
- 如果模型返回空 JSON、无可识别字段或普通闲聊文本，解析器必须抛出 `SummaryError`，由同步流程标记为 `summary_failed`。
- MVP 对超长全文只发送前 30000 字给摘要模型，并在 prompt 中要求模型标注“基于截断原文”；如果模型遗漏，程序会在 `permanent_note` 中本地补上截断说明。

### 9.2 API Adapter

默认按 OpenAI-compatible chat completions 设计：

```text
POST {base_url}/chat/completions
Authorization: Bearer {api_key}
model: deepseek-v4-pro
```

若 OpenCode GO 的 DeepSeek v4 Pro 不兼容，则新增 `OpenCodeGoSummaryService`。

## 10. 转录服务选型

### 10.1 候选方案

| 方案 | 优点 | 风险 | 适配建议 |
| --- | --- | --- | --- |
| faster-whisper | Whisper 系模型，中文长音频准确率通常更稳；CTranslate2 实现，性能好；可 CPU/GPU | 模型较大，安装依赖较重；GPU 环境需额外配置 | 后续优先推荐 |
| openai/whisper | 原版实现，生态成熟，准确率好 | 推理速度和资源占用通常不如 faster-whisper | 作为基准方案 |
| Vosk | 离线、开源、模型小，支持流式和低资源设备；Python/Java/Node/C#/C++/Rust/Go 等绑定 | 中文长内容和复杂口语场景准确率可能不如 Whisper 系；标点和段落质量需后处理 | 作为轻量备选 |
| whisper.cpp | C/C++ 实现，部署轻，CPU 友好 | Python 集成和批处理体验不如 faster-whisper 直接 | Debian 低资源机器可评估 |

### 10.2 结论

优先顺序：

1. `faster-whisper`
2. `whisper.cpp`
3. `Vosk`
4. `openai/whisper`

理由：

- 本项目处理的是得到课程/音频类长内容，中文准确率和段落可读性比实时性更重要。
- faster-whisper 基于 Whisper 系模型，官方 README 声称在相同准确率下比 openai/whisper 更快、内存更省，并支持 int8 量化。
- Vosk 的优势是离线、小模型、低资源和流式，但它更适合作为轻量或嵌入式备选。

实现策略：

- MVP 不实现转录。
- 当前构建中如果 `transcription.enabled: true`，`preflight` 必须失败，避免用户误以为无文字稿内容会自动转录。
- 先定义 `TranscriptionService` 接口。
- 后续接入 `faster-whisper`。
- 若 Windows/Debian 环境资源不足，再评估 Vosk 或 whisper.cpp。

参考：

- faster-whisper GitHub：https://github.com/SYSTRAN/faster-whisper
- Vosk GitHub：https://github.com/alphacep/vosk-api
- openai/whisper GitHub：https://github.com/openai/whisper

## 11. Markdown 写入设计

`MarkdownWriter` 负责：

- 根据栏目创建目录。
- 清洗文件名非法字符。
- 避免重名：同名文件追加短 hash。
- 使用代码内置 renderer 渲染 Markdown，减少 MVP 运行时依赖。
- 写入成功后返回绝对路径。

Windows 非法字符：

```text
< > : " / \ | ? *
```

文件名策略：

```text
{column}-{YYYY-MM-DD}-{title}.md
```

## 12. 飞书通知设计

使用飞书自定义机器人 webhook。

飞书官方文档说明，自定义机器人 webhook 地址形如：

```text
https://open.feishu.cn/open-apis/bot/v2/hook/****
```

开启签名校验时，签名由 timestamp、secret、HmacSHA256 和 Base64 生成。

### 12.1 Notifier 接口

```python
class Notifier:
    def send_run_report(self, report: RunReport) -> None:
        ...
```

### 12.2 通知内容

推荐使用飞书富文本或消息卡片。MVP 可先用文本消息。

示例：

```text
得到同步完成

执行时间：2026-05-26 08:03:12
运行机器：Windows11
耗时：42s
状态：partial_failed
总栏目数：4
发现条目数：15
新增文章数：3
跳过文章数：12
成功文章数：3
失败文章数：1
无文字稿文章数：2
摘要失败数：0

新增内容：
- 尹烨·健康参考：xxx
- 长谈：xxx

失败：
- 马江博-政经参考：登录态失效

无文字稿/待处理：
- 长谈：xxx

摘要失败：
- 尹烨·健康参考：xxx

日志：D:\Project\603_dedao_study\logs\2026-05-26.log
```

### 12.3 失败策略

- 飞书通知失败不影响同步结果。
- 通知失败写入日志。
- `notify-test` 命令用于验证 webhook、secret 和网络可用性。
- 通知不发送全文稿；对 `missing_transcript`/`extractor_failed` 和 `summary_failed` 提供按栏目分组的标题明细，方便从每日通知直接定位后续动作。
- 若 `feishu.include_titles: false`，通知只发送计数、状态和日志路径，不发送新增/失败条目标题或失败明细。
- 若 `feishu.enabled: false`，即使环境变量中存在 webhook，也不发送飞书通知。

参考：

- 飞书自定义机器人使用指南：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
- 飞书消息常见问题：https://open.feishu.cn/document/server-docs/im-v1/faq?lang=zh-CN

## 13. CLI 设计

使用标准库 `argparse`，减少 MVP 运行时依赖。

命令：

```bash
dedao-sync login
dedao-sync check
dedao-sync sync
dedao-sync sync --dry-run
dedao-sync sync --column "长谈"
dedao-sync retry-failed
dedao-sync resummarize
dedao-sync summary-test
dedao-sync notify-test
dedao-sync list
dedao-sync list --runs
dedao-sync list --run-id 1
dedao-sync list --failed
dedao-sync list --status extractor_failed
```

行为：

- `login`：打开浏览器，保存登录态。
- `preflight`：验证配置、vault、登录态、Playwright 依赖；可用 `--no-browser` 只检查非浏览器条件，可用 `--probe-vault-write` 显式测试 Obsidian 输出目录可写。
- `check`：只检查新内容，不写 Markdown，不写入新条目去重库，不发送飞书通知。
- `sync`：完整同步。
- `sync --dry-run`：演练登录、预检查、栏目列表发现和去重判断，但不抓详情、不摘要、不写 Markdown、不发送飞书通知；用于改配置、改栏目列表选择器后的手动验证。
- `sync --column`：只同步指定栏目；如果栏目名没有匹配任何启用栏目，返回 `preflight_failed`，避免静默空跑。
- `retry-failed`：重试失败类条目，包括 `failed`、`extractor_failed`、`missing_transcript`、`summary_failed`、`transcription_failed`。对于已有 `file_path` 的 `summary_failed`，优先从原笔记提取全文并覆盖原文件补摘要，避免重复创建 Markdown。
- `resummarize`：重新生成摘要。
- `summary-test`：用本地样本文稿验证摘要 API、模型返回和解析器。
- `notify-test`：发送测试飞书消息。
- `list`：列出同步记录。
- `list --runs`：列出最近执行历史、计数和日志路径。
- `list --run-id`：列出某次执行记录的条目动作和状态。
- `list --failed`：列出需要处理或重试的失败类条目。
- `list --status`：按指定状态筛选条目，可重复传入。

## 14. 自动化部署

### 14.1 Windows MVP

使用 Windows 任务计划程序：

```powershell
dedao-sync sync
```

前置条件：

- Python venv 已配置。
- Playwright 浏览器已安装。
- `.env` 和 `config.yaml` 已配置。
- 用户已运行 `dedao-sync login`。

### 14.2 Debian 稳定部署

使用 systemd timer：

```text
dedao-sync.service
dedao-sync.timer
```

迁移重点：

- Playwright headless/headful + Xvfb 方案。
- 登录态迁移或 Debian 本机登录。
- Obsidian vault 同步路径。
- faster-whisper 模型缓存路径。

## 15. 日志设计

使用结构化日志：

- 控制台输出简洁进度。
- 文件日志保存详细信息。
- 每日一个日志文件。

日志字段：

- `run_id`
- `column`
- `source_url`
- `item_title`
- `status`
- `duration_ms`
- `error`

安全要求：

- 日志 formatter 会对 token、cookie、API key、secret 和飞书 webhook 做脱敏。
- SQLite 中的 `items.error_message`、`runs.error_message`、`run_items.message` 在写入前同样脱敏。
- `list`、`list --failed`、`list --run-id` 和同步命令的失败输出在展示前再次脱敏，避免历史数据或外部异常消息泄露凭证。

## 16. 错误处理

| 场景 | 处理 |
| --- | --- |
| 登录失效 | 停止抓取，提示重新登录，发送飞书失败通知 |
| 单个栏目失败 | 记录失败，继续其他栏目 |
| 单篇内容失败 | 记录失败，继续其他内容 |
| 摘要失败 | 保存全文，标记 `summary_failed` |
| 飞书失败 | 写日志，不影响同步 |
| 页面结构变化 | 标记 extractor 失败，保留 HTML 片段用于排查 |
| Obsidian 写入失败 | 标记失败，不更新为 synced |

页面结构调试补充：

- `save_failure_html` 默认关闭，避免自动保存会员可见页面 HTML。
- 调试期临时开启后，详情页正文提取失败时会保存 HTML 到 `failure_snapshot_dir`，并把路径写入 `items.error_message` 和 `run_items.message`。
- `data/page_failures/` 必须加入 `.gitignore`，保存的 HTML 只用于本地排查，不进入仓库或通知正文。

## 17. 合规边界

- 只访问用户本人会员/购买后可见内容。
- 不绕过验证码、风控、DRM 或加密媒体。
- 不实现公开分享、批量外传或内容再发布。
- 请求保持低频，默认串行。
- 遇到访问限制时停止，不尝试规避。

## 18. 分阶段实施

### Phase 1：文字稿同步 MVP

- 项目骨架
- 配置加载
- Playwright 登录态保存
- 四个栏目列表解析
- 详情页文字稿提取
- SQLite 去重
- Markdown 写入
- 飞书通知

### Phase 2：摘要服务

- OpenCode GO / DeepSeek v4 Pro adapter
- Zettelkasten prompt
- 摘要失败重试
- `resummarize` 命令

### Phase 3：转录

- 媒体候选识别
- 合规下载检查
- faster-whisper 接入
- 转录稿后处理
- 媒体缓存删除

### Phase 4：Debian 常驻

- Debian 部署文档
- systemd timer
- 登录态迁移方案
- vault 同步方案
- 转录性能调优

## 19. 仍需确认

- OpenCode GO 的 API base URL、模型名、请求/响应格式。
- 飞书机器人是否开启签名校验。
- Windows 和 Debian 的硬件配置，尤其是 GPU 和内存。
- 是否允许保存原始 HTML 片段用于调试。
- Debian 与 `D:\biji\openclaw-vault` 的最终同步方式。

## 20. 策略修正与置信门槛

详细红队审查记录见 `docs/STRATEGY_REVIEW.md`。

Phase 1 进入开发前采用以下修正：

- MVP 只做 Windows 本地网页文字稿同步，不做转录，不迁移 Debian。
- 增加 `PreflightChecker`，在同步前检查配置、vault 路径、数据库、登录态、飞书配置。
- `PreflightChecker` 会检查配置语义：至少一个启用栏目、栏目名不重复、栏目 URL 合法、请求间隔非负、摘要 provider 受支持、文件名模板包含必需字段。
- `doctor` 复用同一套配置语义检查，并在 JSON 输出中给出 `config_semantics` 项，便于定时任务或外部监控判断配置是否已坏。
- `PreflightChecker` 默认不写入 vault；需要确认目标目录可写时，可显式启用写入探针，创建并删除 Obsidian 输出目录中的临时文件。
- 每次运行必须创建 `runs` 记录，不能只依赖日志文件。
- 抓取前先验证登录态；登录失效直接停止抓取并通知。
- 栏目页解析为空时不直接视为成功，需要区分“确实无更新”和“解析失败”。
- 正文提取必须通过质量门槛，包括最小长度、最小段落数、标题相关性、UI 噪声比例。
- 正文质量门槛失败时标记 `extractor_failed`，不写 Markdown。
- Markdown frontmatter 使用 YAML serializer，不手写拼接。
- Markdown 写入采用临时文件 + 原子移动，写入成功后再更新 DB。
- 去重使用 `source_url`、canonical URL、栏目内 `dedao_id`、`content_hash` 多重判断。
- 飞书通知只发运行摘要和标题，不发送全文。
- 所有 secret、cookie、token、webhook URL 都做日志脱敏。
- Debian 迁移前要求 Windows MVP 连续稳定运行 7 天。

新增状态：

- `extractor_failed`
- `preflight_failed`
- `login_required`
- `locked`

置信门槛：

- 已知 P0 风险必须有阻断机制。
- 已知 P1 风险必须有检测、降级或恢复机制。
- 页面结构变化不能静默污染 Obsidian 笔记。
- 飞书失败不能影响主流程。
- 摘要失败不能影响全文保存。
- 定时任务和手动运行不能并发写入；`sync`、`retry-failed`、`resummarize` 使用项目级运行锁。
