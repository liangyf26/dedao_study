# 得到内容同步到 Obsidian

按 `docs/PRD.md` 和 `docs/TECH_DESIGN.md` 开发中的本地 Python 项目。

当前实现聚焦 Phase 1 的地基：

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

还需要真实登录后验证得到页面结构，并补强四个栏目对应的页面选择器。当前 crawler 已有通用 anchor 发现入口，但尚未用登录态样本证明可稳定解析真实栏目。

正文提取已加入质量门槛和候选块选择：优先从 `article`、`main`、`section` 中选择干净正文，避免把登录、分享、推荐等页面噪声写进 Obsidian。

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

更完整的本地运行步骤见 [RUNTIME_SETUP.md](D:/Project/603_dedao_study/docs/RUNTIME_SETUP.md)。

Windows 定时任务设置见 [SCHEDULING.md](D:/Project/603_dedao_study/docs/SCHEDULING.md)。

常用命令：

```powershell
dedao-sync preflight --config config.yaml
dedao-sync doctor --config config.yaml
dedao-sync doctor --config config.yaml --json
dedao-sync login --config config.yaml
dedao-sync inspect-page --config config.yaml "https://www.dedao.cn/course/detail?id=..."
dedao-sync check --config config.yaml
dedao-sync sync --config config.yaml --dry-run
dedao-sync sync --config config.yaml
dedao-sync retry-failed --config config.yaml
dedao-sync resummarize --config config.yaml
dedao-sync summary-test --config config.yaml
dedao-sync list --config config.yaml --runs
dedao-sync list --config config.yaml --run-id 1
dedao-sync list --config config.yaml --failed
dedao-sync notify-test --config config.yaml
```

`check` 会访问栏目列表并统计新内容，但不会写 Markdown，不会把新条目写入去重库，也不会发送飞书通知。`sync --dry-run` 同样不会写 Markdown 或发送飞书通知，适合在改配置、改栏目列表选择器后演练发现和去重流程。

`inspect-page` 会把页面 HTML 和可见文本保存到 `data/page_snapshots/`，用于登录后调试真实得到页面结构。

`dedao.save_failure_html` 默认关闭；调试正文提取失败时可临时启用，程序会把失败详情页 HTML 保存到 `data/page_failures/`，并把路径写进失败记录。该目录已加入 `.gitignore`，因为其中可能包含会员可见内容。

`parse-snapshot --show-candidates` 可以离线查看正文候选的质量评分，帮助判断真实页面解析失败的原因。`parse-snapshot --show-items` 可以查看栏目页中被识别为内容条目的链接。`parse-snapshot --json` 会输出机器可读的解析报告，方便保存真实快照的回归基线。

`list --runs` 会列出最近几次执行的状态、发现/新增/成功/失败计数和日志路径，用于排查每日定时任务结果。

`list --run-id <id>` 会列出某次执行里每篇内容的动作、状态和错误/文件路径，用于从一条 `partial_failed` 运行追到具体条目。

`runs.status` 只有在失败、无文字稿和摘要失败计数都为 0 时才会显示 `success`；只要存在需要处理的条目，就会显示 `partial_failed`。

`preflight` 和 `doctor` 不只检查登录态文件是否存在，也会检查它是否是有效的 Playwright `storage_state` JSON，并且包含 cookies 或 origins。空文件、坏 JSON 或 `{}` 会提示重新运行 `dedao-sync login`。

`list --failed` 会列出需要处理或重试的条目，包括 `failed`、`extractor_failed`、`missing_transcript`、`summary_failed` 和 `transcription_failed`。

`retry-failed` 会重试上述失败类条目；对已有 `file_path` 的 `summary_failed` 条目会原地覆盖补摘要，不会创建第二份笔记。后续转录模块接入后，`transcription_failed` 也会进入同一恢复路径。

`summary-test` 会用一段本地样本文稿调用摘要模型，验证 OpenCode GO/DeepSeek 配置、网络和返回格式是否可用。

`summary_failed` 不代表全文同步失败；如果 `file_path`、`has_transcript=1` 和 `synced_at` 存在，说明正文已写入 Obsidian，只需要后续 `resummarize` 或 `retry-failed` 补摘要。

飞书通知不会发送全文；除了计数和新增标题，也会列出无文字稿/待处理条目和摘要失败条目，方便从通知直接定位后续动作。若设置 `feishu.include_titles: false`，通知会隐藏条目标题和失败明细，只保留计数与日志路径。

当前构建尚未接入真实转录引擎，`transcription.enabled` 需要保持 `false`；设为 `true` 会让 `preflight` 失败。
