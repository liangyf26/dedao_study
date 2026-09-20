# 得到内容同步到 Obsidian

按 `docs/PRD.md` 和 `docs/TECH_DESIGN.md` 开发中的本地 Python 项目。

当前实现包括：

- 配置加载与环境变量读取
- SQLite 状态库
- Markdown 原子写入
- 飞书通知 payload、签名和发送
- 登录态/preflight 检查
- 内容模型、摘要/转录接口占位
- Playwright crawler 接入入口
- `sync`、`retry-failed`、`resummarize`、`summary-test`、`notify-test` CLI
- `doctor` 运行环境诊断
- 详情页标题、作者、发布日期元数据抽取，并写入 Markdown frontmatter 与 SQLite
- 日志、状态库错误字段、运行明细和 CLI 输出中的 token/cookie/webhook 脱敏
- JSON 优先的 Zettelkasten 摘要解析，Markdown 章节兜底
- 标准库测试覆盖幂等写入、正文 hash 去重、跨栏目 ID 碰撞、失败重试和重摘要

当前配置包含六个栏目：快刀青衣·快刀广播站、尹烨·健康参考、马江博·政经参考、脱不花·长谈、得到头条、得到精选。同步使用保存的登录态读取列表和正文，再生成摘要及 Obsidian 笔记。

正文提取已加入质量门槛和候选块选择：优先从 `article`、`main`、`section` 中选择干净正文，避免把登录、分享、推荐等页面噪声写进 Obsidian。

## 栏目与按年保存

`config.example.yaml` 包含六个启用的栏目。2026-09-20 新增：

| 栏目 | 课程 ID | 保存目录（相对 Obsidian vault） |
| --- | --- | --- |
| 得到头条 | `nb9L2q1e3OxKBPNsdoJrgN8P0Rwo6B` | `5-收件箱(Inbox)/得到/得到头条/YYYY/` |
| 得到精选 | `b0rNAzaYOj7VyPMs09K8P54m6wlk12` | `5-收件箱(Inbox)/得到/得到精选/YYYY/` |

设置 `obsidian.year_subfolders: true` 后，笔记直接写入“栏目/文章发表年份/”目录，
例如 `得到/得到头条/2026/得到头条-2026-09-20-标题.md`。年份取自文章发布日期，
不是抓取日期；缺少有效日期时暂存栏目根目录。数据库保存实际文件路径，手动同步与
定时任务行为一致。省略该选项的旧配置继续使用原有扁平目录。

`config.yaml` 不提交 Git，升级已有部署时需要将新栏目和 `year_subfolders` 设置合并进
本地配置；不要用模板覆盖已有路径、摘要模型或通知配置。

按栏目验证（每个栏目最多新增一篇，实际生成摘要并保存）：

```bash
.venv/bin/dedao-sync sync --config config.yaml --column "得到头条" --limit 1
.venv/bin/dedao-sync sync --config config.yaml --column "得到精选" --limit 1
```

每日 `sync` 自动遍历全部启用栏目。首次加入栏目时，数据库里没有记录的历史文章也会
被视为新增；不加 `--limit` 时会逐篇抓取并调用摘要模型，首次运行可能明显长于平日。
列表按网站顺序读取到最后一页。长栏目不再在 50 轮翻页后静默截断；程序保留 1,000 轮
保护上限，达到上限仍有下一页时会明确报错，不会将残缺列表当作同步成功。
只有网站提供、且账号有权访问的内容能够同步。

## 快速开始

```powershell
py -m dedao_sync.cli init
py -m unittest discover -s tests
py -m dedao_sync.cli --help
```

Windows 一键准备环境：

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\bootstrap_windows.ps1
```

真实同步前需要复制并填写：

```text
config.example.yaml -> config.yaml
.env.example -> .env
```

首次网页登录：

```powershell
dedao-sync login
```

完整网页抓取依赖 Playwright，安装依赖后还需要执行：

```powershell
playwright install chromium
```

PyYAML 不是 MVP 必需依赖；项目内置了覆盖当前配置模板的有限 YAML 解析器。若后续希望使用更复杂的 YAML 写法，可安装 `dedao-sync[yaml]`。

更完整的本地运行步骤见 [RUNTIME_SETUP.md](docs/RUNTIME_SETUP.md)。

Windows 定时任务设置见 [SCHEDULING.md](docs/SCHEDULING.md)。

定时任务 wrapper 会额外写 `logs/scheduled-YYYY-MM-DD.log`，用于排查 Python 启动前的任务计划失败、虚拟环境路径错误或工作目录异常。

Debian systemd 常驻部署准备说明见 [DEBIAN_DEPLOY.md](docs/DEBIAN_DEPLOY.md)。建议 Windows MVP 连续稳定运行 7 天后再迁移。

常用命令：

```powershell
dedao-sync preflight --config config.yaml
dedao-sync doctor --config config.yaml
dedao-sync doctor --config config.yaml --json
dedao-sync login --config config.yaml
dedao-sync inspect-page --config config.yaml "https://www.dedao.cn/course/detail?id=..."
dedao-sync check --config config.yaml
dedao-sync sync --config config.yaml --dry-run
dedao-sync sync --config config.yaml --limit 3 --no-summary
dedao-sync sync --config config.yaml
dedao-sync retry-failed --config config.yaml
dedao-sync resummarize --config config.yaml
dedao-sync resummarize --config config.yaml --all
dedao-sync summary-test --config config.yaml
dedao-sync list --config config.yaml --runs
dedao-sync list --config config.yaml --run-id 1
dedao-sync list --config config.yaml --failed
dedao-sync notify-test --config config.yaml
```

`check` 会访问栏目列表并统计新内容，但不会写 Markdown，不会把新条目写入去重库，也不会发送飞书通知。`sync --dry-run` 同样不会写 Markdown 或发送飞书通知，适合在改配置、改栏目列表选择器后演练发现和去重流程。首次正式同步可用 `sync --limit 3` 小批量处理；如果摘要服务临时不可用，可加 `--no-summary` 先写入全文稿，后续再用 `resummarize` 或 `retry-failed` 补摘要。

`inspect-page` 会把页面 HTML 和可见文本保存到 `data/page_snapshots/`，用于登录后调试真实得到页面结构。

`dedao.save_failure_html` 默认关闭；调试正文提取失败时可临时启用，程序会把失败详情页 HTML 保存到 `data/page_failures/`，并把路径写进失败记录。该目录已加入 `.gitignore`，因为其中可能包含会员可见内容。

`parse-snapshot --show-candidates` 可以离线查看正文候选的质量评分，帮助判断真实页面解析失败的原因。`parse-snapshot --show-items` 可以查看栏目页中被识别为内容条目的链接。`parse-snapshot --json` 会输出机器可读的解析报告，方便保存真实快照的回归基线。

详情页解析会识别网页中正常暴露的音频/视频候选，例如 `<audio>`、`<video>`、`<source>` 和 `og:audio`/`og:video` 元数据。MVP 不下载媒体也不转录，但无文字稿记录会包含媒体候选数量和类型，方便后续接入转录。

如果页面或媒体候选出现 DRM、加密媒体或加密流信号，程序会记录为 `policy_blocked`，不写入 Obsidian，也不会自动重试。这类条目可用 `dedao-sync list --failed` 查看后人工判断。

`list --runs` 会列出最近几次执行的状态、发现/新增/跳过/成功/网页请求/失败/无文字稿/摘要失败计数和日志路径，用于排查每日定时任务结果。

`list --run-id <id>` 会列出某次执行里每篇内容的动作、状态和错误/文件路径，用于从一条 `partial_failed` 运行追到具体条目。

`runs.status` 只有在失败、无文字稿和摘要失败计数都为 0 时才会显示 `success`；只要存在需要处理的条目，就会显示 `partial_failed`。

`preflight` 和 `doctor` 不只检查登录态文件是否存在，也会检查它是否是有效的 Playwright `storage_state` JSON，并且包含 cookies 或 origins。空文件、坏 JSON 或 `{}` 会提示重新运行 `dedao-sync login`。

`list --failed` 会列出需要处理或重试的条目，包括 `failed`、`extractor_failed`、`missing_transcript`、`summary_failed`、`transcription_failed` 和需要人工判断的 `policy_blocked`。

`retry-failed` 会重试可恢复的失败类条目；对已有 `file_path` 的 `summary_failed` 条目会原地覆盖补摘要，不会创建第二份笔记。后续转录模块接入后，`transcription_failed` 也会进入同一恢复路径。`policy_blocked` 不会自动重试。

`resummarize` 默认只处理摘要缺失或失败的笔记；当你调整摘要 prompt 或模型后，可用 `resummarize --all` 刷新所有已有全文稿的摘要。

`summary-test` 会用一段本地样本文稿调用摘要模型，验证 OpenCode GO/DeepSeek 配置、网络和返回格式是否可用。

`summary_failed` 不代表全文同步失败；如果 `file_path`、`has_transcript=1` 和 `synced_at` 存在，说明正文已写入 Obsidian，只需要后续 `resummarize` 或 `retry-failed` 补摘要。

飞书通知不会发送全文；除了计数和新增标题，也会列出无文字稿/待处理条目和摘要失败条目，方便从通知直接定位后续动作。若设置 `feishu.include_titles: false`，通知会隐藏条目标题和失败明细，只保留计数与日志路径。若 `feishu.enabled: true`，正式 `sync`、`retry-failed` 和 `resummarize` 会要求 webhook 环境变量存在；`check` 和 `sync --dry-run` 不发送通知，也不强制要求 webhook。

当前构建尚未接入真实转录引擎，`transcription.enabled` 需要保持 `false`；设为 `true` 会让 `preflight` 失败。媒体候选只作为后续转录线索记录，不代表已经下载或处理媒体文件。

## 登录失效的恢复

得到的登录会话会过期。未登录的“我的学习”页面仍可能显示“最近学习”，
同步程序会同时检查独立的“登录 / 注册”入口，避免把这个空页面误判为已登录。
失效时任务会在抓取前报告 `login_required`。

在项目目录执行 `.venv/bin/dedao-sync login --config config.yaml`，完成浏览器登录后
回终端按 Enter 保存。随后执行 `.venv/bin/dedao-sync retry-failed --config config.yaml`
补抓失败条目，再执行 `systemctl --user start dedao-sync.service` 验证完整定时流程。

普通 `sync` 会跳过数据库中已登记的条目（包括失败记录），因此恢复登录后必须先执行
`retry-failed`；仅等待下一次每日同步不会补抓这些历史失败项。
建议每 25 天主动检查并更新登录，在会话标注到期前留出余量。服务端可能提前撤销会话，
每日同步仍会做在线登录检查，并通过已配置的飞书通知报告失效；请关注
`login_required`、`partial_failed`，以及每天预期时间后没有收到运行结果的情况。

Linux 定时脚本将笔记移入年份目录后，会核对正文哈希或来源信息，并同步校正数据库
中的文件路径，避免后续补摘要找不到已归档的笔记。
