# 本地运行设置

最后更新：2026-05-27

## 1. 初始化项目配置

在项目根目录执行：

```powershell
py -m dedao_sync.cli init
```

该命令会创建：

- `config.yaml`
- `.env`
- `data/auth/`
- `logs/`

如果文件已存在，默认不会覆盖。需要覆盖时使用：

```powershell
py -m dedao_sync.cli init --force
```

## 2. 创建虚拟环境

```powershell
py -3.13 -m venv .venv
```

也可以使用一键 bootstrap 脚本：

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\bootstrap_windows.ps1
```

该脚本会初始化配置、创建 `.venv`、安装项目依赖、安装 Playwright Chromium，并运行 `doctor`。

## 3. 安装依赖

```powershell
.venv\Scripts\python.exe -m pip install -e .[dev]
```

如果网络受限，pip 可能无法下载依赖。需要允许访问 Python 包索引后重试。

安装 Playwright 浏览器：

```powershell
.venv\Scripts\python.exe -m playwright install chromium
```

## 4. 配置密钥

编辑 `.env`：

```dotenv
OPENCODE_GO_BASE_URL=
OPENCODE_GO_API_KEY=
FEISHU_WEBHOOK_URL=
FEISHU_WEBHOOK_SECRET=
```

`FEISHU_WEBHOOK_SECRET` 只有在飞书机器人开启签名校验时才需要填写。

如果不希望飞书通知里出现新增内容标题或失败条目标题，可以在 `config.yaml` 中设置：

```yaml
feishu:
  include_titles: false
```

如果要完全关闭飞书通知，设置：

```yaml
feishu:
  enabled: false
```

## 5. 首次登录

```powershell
.venv\Scripts\dedao-sync.exe login --config config.yaml
```

浏览器打开后手动登录得到。登录完成后回到终端按 Enter，程序会保存登录态到 `data/auth/dedao_state.json`。

`doctor` 和 `preflight` 会检查该文件是否是有效的 Playwright `storage_state` JSON，并且至少包含 cookies 或 origins。若文件为空、损坏或只是 `{}`，请重新执行登录命令。

## 6. 页面结构调试

登录后先保存四个栏目页面快照：

```powershell
.venv\Scripts\dedao-sync.exe inspect-page --config config.yaml "https://www.dedao.cn/course/detail?id=zp9lB3q0breKZq4sDWXYjyWxG64dg2"
```

快照会保存到：

```text
data/page_snapshots/
```

该目录已加入 `.gitignore`，因为页面快照可能包含登录后可见内容。

如果同步时正文提取失败，需要保留失败详情页 HTML 便于离线分析，可临时在 `config.yaml` 中启用：

```yaml
dedao:
  save_failure_html: true
  failure_snapshot_dir: "data/page_failures"
```

该开关默认关闭。保存的 HTML 可能包含会员可见内容，仅用于本地调试；`data/page_failures/` 已加入 `.gitignore`。失败记录和 `list --failed` / `list --run-id` 会显示对应 HTML 路径。

离线检查快照质量：

```powershell
.venv\Scripts\dedao-sync.exe parse-snapshot data/page_snapshots/example.html --title "标题" --column "栏目" --url "原始URL" --write-transcript
```

查看每个正文候选的质量诊断：

```powershell
.venv\Scripts\dedao-sync.exe parse-snapshot data/page_snapshots/example.html --title "标题" --column "栏目" --url "原始URL" --show-candidates
```

查看栏目页中被识别为内容条目的链接：

```powershell
.venv\Scripts\dedao-sync.exe parse-snapshot data/page_snapshots/example.html --title "标题" --column "栏目" --url "原始URL" --show-items
```

生成机器可读解析报告：

```powershell
.venv\Scripts\dedao-sync.exe parse-snapshot data/page_snapshots/example.html --title "标题" --column "栏目" --url "原始URL" --json
```

候选诊断会显示：

- `ok`：该候选是否通过质量门槛
- `reason`：失败原因，例如 `too_short`、`too_much_ui_noise`、`title_not_related`
- `chars`：候选文本长度
- `paragraphs`：段落数
- `noise`：登录、分享、购买等 UI 噪声命中次数
- `preview`：候选文本预览

带 `*` 的候选是程序最终选择的正文。

`--json` 会输出 `has_transcript`、`quality_reason`、`transcript_chars`、正文候选诊断和条目候选列表，适合把四个真实栏目快照的解析结果保存下来做回归比较。

`inspect-page` 会同时保存：

- `.html`：页面 HTML
- `.txt`：页面可见文本
- `.anchors.json`：页面中所有链接的文本和 URL，方便调试栏目列表解析

正文提取会优先从 `article`、`main`、`section` 等正文容器中挑选质量最高的候选文本。如果候选正文太短、段落太少、登录/分享/购买等 UI 噪声过高，或者与标题明显无关，程序会标记为提取失败，不会写入 Obsidian。

摘要服务会要求模型输出 JSON，程序再渲染为 Obsidian 中的卡片笔记结构。解析器也兼容模型偶发输出的 Markdown 章节，但稳定运行时应优先使用 JSON 输出。

超长全文在 MVP 中只会发送前 30000 字给摘要模型。程序会要求模型在永久笔记中标注“基于截断原文”；如果模型遗漏，程序会本地补上该说明。

## 7. 同步流程

先检查：

```powershell
.venv\Scripts\dedao-sync.exe doctor --config config.yaml
.venv\Scripts\dedao-sync.exe doctor --config config.yaml --json
.venv\Scripts\dedao-sync.exe preflight --config config.yaml
.venv\Scripts\dedao-sync.exe check --config config.yaml
.venv\Scripts\dedao-sync.exe sync --config config.yaml --dry-run
```

`preflight` 默认会检查 Playwright 是否已安装。如果只是验证配置文件和 vault 路径，可以临时加 `--no-browser`。如需确认 Obsidian 输出目录可写，可额外加 `--probe-vault-write`，它会创建并删除一个临时探针文件；同步盘较慢时不建议把该选项放进每日定时任务。

`check` 是手动检查命令，只访问栏目列表并统计新内容；它不会写 Markdown、不会把新条目写入去重库，也不会发送飞书通知。`sync --dry-run` 也不会写 Markdown 或发送飞书通知，适合在改配置、改栏目列表选择器后演练发现和去重流程。

当前版本还没有真正接入转录引擎。请保持 `transcription.enabled: false`；如果改成 `true`，`preflight` 会失败，避免误以为无文字稿内容已经能自动转录。

`doctor` 和 `preflight` 都会检查栏目配置和文件命名模板：至少一个栏目启用、栏目名不重复、栏目 URL 是 `http(s)`、请求间隔非负、`summary.provider` 是当前支持的 `opencode_go`，以及 `filename_pattern` 包含 `{column}`、`{published_date}`、`{title}`。

正式同步：

```powershell
.venv\Scripts\dedao-sync.exe sync --config config.yaml
```

失败重试：

```powershell
.venv\Scripts\dedao-sync.exe retry-failed --config config.yaml
```

`retry-failed` 会处理 `failed`、`extractor_failed`、`missing_transcript`、`summary_failed` 和 `transcription_failed`。如果 `summary_failed` 条目已有 `file_path` 且全文仍在原笔记中，命令会原地覆盖补摘要，不会创建第二份 Markdown。

重跑摘要：

```powershell
.venv\Scripts\dedao-sync.exe resummarize --config config.yaml
```

摘要服务测试：

```powershell
.venv\Scripts\dedao-sync.exe summary-test --config config.yaml
```

`summary-test` 会用一段本地样本文稿调用当前配置的摘要模型，验证 OpenCode GO base URL、API key、模型返回格式和解析器是否可用。

当全文已经成功写入但摘要失败时，条目状态会是 `summary_failed`，同时保留 `file_path`、`has_transcript=1` 和 `synced_at`。这表示正文笔记已经在 Obsidian 中，后续用 `resummarize` 或 `retry-failed` 补摘要即可。

查看最近执行历史：

```powershell
.venv\Scripts\dedao-sync.exe list --config config.yaml --runs
```

查看某次执行的条目明细：

```powershell
.venv\Scripts\dedao-sync.exe list --config config.yaml --run-id 1
```

查看需要处理或重试的条目：

```powershell
.venv\Scripts\dedao-sync.exe list --config config.yaml --failed
```

飞书通知测试：

```powershell
.venv\Scripts\dedao-sync.exe notify-test --config config.yaml
```

如果通知失败，命令会返回非零并输出 `notification failed: ...`。常见原因包括当前终端不能访问外网、webhook/secret 配置错误，或飞书机器人安全策略未放行。

飞书通知只发送运行摘要、标题列表、失败摘要和日志路径，不发送全文稿。通知中会包含运行机器、执行时间、耗时、总栏目数、新增/跳过/失败/无文字稿/摘要失败计数，并按栏目列出无文字稿/待处理条目和摘要失败条目。若 `feishu.include_titles: false`，通知会隐藏这些条目标题和失败明细，只保留计数与日志路径。

如果飞书通知遗漏或定时任务静默失败，优先用 `list --runs` 查看最近执行状态，再根据输出中的 `log=` 路径打开对应日志。若某次执行是 `partial_failed`，可用 `list --run-id <id>` 查看该次执行的条目动作，也可用 `list --failed` 查看仍需处理的失败类条目。

## 8. Windows 定时任务

定时运行说明见：

```text
docs/SCHEDULING.md
```

## 9. 当前环境记录

本仓库当前已创建 `.venv`，但依赖安装在当前沙箱环境中被网络权限拦截。失败信息是 pip 无法连接包索引。后续在允许网络访问的终端中重新执行第 3 步即可。
