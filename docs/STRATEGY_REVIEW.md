# 策略红队审查与修正记录 v0.1

最后更新：2026-05-28

## 1. 结论

我对原策略没有字面意义的 100% 信心。原因是项目依赖第三方网页、登录态、页面结构、飞书 webhook、外部大模型 API、未来的本地转录环境，以及得到平台自身规则变化。这类系统无法通过设计消除所有不确定性。

但可以把策略修正到“事实上的高置信”：没有已知未缓解的 P0/P1 风险；所有高概率失败点都有检测、降级、告警和人工恢复路径；MVP 范围内不把未知问题变成上线阻塞。

## 2. 置信标准

后续开发进入实现前，需要满足以下门槛：

- P0 风险数量为 0。
- P1 风险都必须有明确缓解措施、检测方式和恢复方式。
- MVP 不依赖未验证的转录方案。
- MVP 不依赖 Debian 常驻运行。
- 飞书通知失败不得影响主流程。
- 登录态失效必须能被检测并通知。
- 页面结构变化必须能被检测，不允许静默写入错误笔记。
- Obsidian 写入必须幂等，不允许重复创建同一内容。

## 3. 风险等级

- P0：可能导致合规、安全、账号、数据损坏或不可恢复问题。
- P1：可能导致核心同步功能不可用。
- P2：可能导致体验下降、成本上升或局部失败。
- P3：可接受的小问题或后续优化。

## 4. 第一轮：漏洞清单与修复措施

| 风险 | 等级 | 漏洞描述 | 修复措施 | 验证方式 |
| --- | --- | --- | --- | --- |
| 合规边界不够硬 | P0 | 媒体下载、转录、自动化访问可能滑向绕过限制 | 明确只处理用户正常网页可访问内容；遇到 DRM、加密、验证码、风控直接停止 | 在 PRD/技术设计中保留硬边界；代码中加入 policy check |
| 登录态泄露 | P0 | cookie/storage_state 可直接代表登录身份 | `.env`、`data/auth`、浏览器 profile、日志默认入 `.gitignore`；日志脱敏 | 检查 `.gitignore`；扫描日志不含 cookie/token |
| 敏感路径被配置到项目内未忽略位置 | P0 | `auth_state_path`、浏览器 profile、失败 HTML 或媒体缓存误配到项目根目录，可能被 Git 看到 | 若敏感路径位于项目目录内，preflight/doctor 强制其留在默认已忽略的 `data/...` 目录；项目外路径允许 | Preflight 和 doctor 单元测试覆盖项目内误配 |
| 错误内容写入 Obsidian | P1 | 页面结构变化时可能把导航、评论、推荐流当正文 | extractor 必须有正文质量门槛：最小长度、段落数、黑名单 UI 文本比例、标题匹配 | 用样本页跑 extractor snapshot test |
| 静默失败 | P1 | 每天任务失败但用户不知道 | run 级别状态记录；飞书通知包含失败摘要；飞书失败写日志 | `notify-test` 和模拟异常测试 |
| 登录失效误判 | P1 | 页面返回登录页但程序当作内容页解析 | 每次运行先访问登录态验证页；检测登录按钮、二维码、401/403、会员提示 | mock/手动过期登录态测试 |
| 重复笔记 | P1 | URL 变化或标题变化导致重复保存；跨栏目 `dedao_id` 碰撞可能误跳过内容 | 使用 `source_url`、canonical URL、栏目内 `dedao_id`、详情页 `content_hash` 多重去重；写文件前查 DB | 重复运行和跨栏目同 ID 测试 |
| 文件覆盖 | P1 | 同名标题导致覆盖旧文件 | 默认不覆盖；同名追加短 hash；DB 记录实际路径 | 同标题样本测试 |
| Windows 路径问题 | P1 | 标题含非法字符、保留名、路径过长 | 文件名清洗、长度截断、保留名规避、路径长度检查 | 单元测试非法文件名 |
| Obsidian vault 路径不可用 | P1 | D 盘路径不存在、同步软件锁文件 | 启动前 preflight 检查；写入临时文件后原子替换 | 缺路径和只读目录测试 |
| Obsidian 输出目录配置逃逸 vault | P1 | `output_dir` 配成绝对路径或 `..` 时可能把笔记写到 vault 外 | `obsidian.output_dir` 必须是 vault 内部相对路径，preflight/doctor 阶段拦截 | Preflight 和 doctor 单元测试覆盖 `../outside` 与绝对路径 |
| API Key 泄露 | P0 | DeepSeek/OpenCode GO key 进入日志或 Markdown | 统一 Secret 类型，日志脱敏；`.env` 不入库 | 日志扫描测试 |
| 摘要成本和隐私 | P1 | 全文发送到外部 API，可能触发成本或隐私问题 | 摘要独立开关；显示 provider；支持只保存全文；可限制最大 tokens | 配置禁用摘要测试 |
| 摘要幻觉 | P2 | 模型生成并非原文观点的内容 | prompt 要求区分事实/判断/启发；输出“需回看原文确认”；摘要不能覆盖全文 | 人工抽样 QA |
| 飞书泄露内容 | P1 | 通知里发送过多标题或敏感摘要 | 飞书只发运行摘要和标题，不发全文；可配置隐藏标题 | 通知内容快照测试 |
| 飞书签名错误 | P2 | 签名算法或时间戳错误导致通知失败 | 单独 `notify-test`；签名模块单元测试 | 用真实 webhook 测试 |
| 页面加载不稳定 | P1 | SPA 异步加载导致列表为空 | 使用可见内容等待、网络 idle、重试和超时；空列表不等于成功 | 多次重复 check |
| 请求频率过高 | P0 | 触发风控或违反合理使用 | 默认串行、间隔 2s+ 抖动、每日一次；不做大规模并发 | 日志记录请求次数 |
| Obscura 兼容性未知 | P2 | 过早采用导致登录/页面行为异常 | MVP 固定 Playwright + Chromium；Obscura 只作为后续实验 backend | 不进入 Phase 1 |
| 转录方案过早绑定 | P2 | Whisper/Vosk 环境和准确率未验证 | MVP 不实现转录；只记录 `missing_transcript`；后续用样本集评估 | Phase 3 前做 ASR benchmark |
| 媒体下载风险 | P0 | 下载加密/受保护媒体可能越界 | 仅处理浏览器页面正常暴露且可下载的媒体；遇到加密流停止 | policy check + 日志 |
| Debian 迁移过早 | P1 | 登录态、vault 同步、浏览器环境都可能不稳 | Phase 4 才迁移；设置准入：Windows MVP 连续稳定运行 7 天 | 7 日运行报告 |
| 数据库损坏 | P1 | 任务中断导致状态不一致 | SQLite 事务；先写文件再提交 synced；定期备份 DB | 中断恢复测试 |
| Markdown frontmatter 破坏 | P2 | 标题含冒号、引号、换行或控制字符导致 YAML 无效 | 使用内置 YAML-safe scalar renderer 统一转义 frontmatter 标量 | Markdown 单元测试覆盖危险字符转义 |
| 时区和日期错误 | P2 | 发布时间/同步时间混淆 | 所有运行时间用 Asia/Shanghai；原始发布时间保留字符串和解析值 | 日期样本测试 |
| 内容超长导致摘要失败 | P2 | 模型上下文超限；截断后摘要可能被误认为覆盖全文 | 分块摘要 + 合并摘要；MVP 先截断、要求摘要标注“基于截断原文”，并在模型遗漏时本地补标注 | 超长文本测试 |
| 摘要假成功 | P1 | 模型返回空 JSON、闲聊文本或无可识别字段时，系统可能当作成功 | 摘要解析结果必须至少包含一个有效字段，否则抛 `SummaryError` 并标记 `summary_failed` | 空 JSON 和非结构化文本测试 |

第一轮结论：原策略可行，但必须补上 preflight、质量门槛、幂等写入、脱敏、状态机和阶段准入，否则不应直接开发完整自动化。

## 5. 第二轮：修正后的策略

修正后的 MVP 策略：

1. 只做 Windows 本地文字稿同步，不做转录，不迁移 Debian。
2. 每次运行先做 preflight：配置、vault 路径、数据库、登录态、飞书配置。
3. 每次运行创建 `runs` 记录，所有条目有状态，不允许只有日志没有数据库记录。
4. 抓取前验证登录态；登录失效直接停止抓取并通知。
5. 栏目页解析为空时不视为成功，需要区分“确实无更新”和“页面解析失败”。
6. 正文提取必须通过质量门槛，否则标记 `extractor_failed`，不写 Markdown。
7. Markdown 写入采用临时文件 + 原子移动；成功后再更新 DB。
8. 去重使用 `source_url || canonical_url || 栏目内 dedao_id || content_hash`，不是只靠标题，也不把 `dedao_id` 当全局唯一。
9. 摘要模块可关闭；摘要失败不影响全文保存。
10. 飞书通知只发运行摘要和标题，不发全文。
11. 所有 secret、cookie、token、webhook URL 都做日志脱敏。
12. 连续稳定运行 7 天后，再进入 Debian 迁移讨论。
13. `runs.status` 只在失败、无文字稿、摘要失败计数都为 0 时才是 `success`；否则为 `partial_failed`，避免每日通知漏看需要处理的条目。

## 6. 第三轮：剩余风险与接受条件

修正后仍无法完全消除的风险：

- 得到网页结构或访问策略随时可能变化。
- 得到可能改变登录态有效期或触发重新验证。
- 外部摘要 API 的兼容性、限额、价格和可用性可能变化。
- 飞书 webhook 可能因安全策略、网络或机器人配置失败。
- 后续转录涉及媒体可访问性和合规边界，必须单独评估。

这些风险无法通过本地设计完全消除，但可以通过以下方式接受：

- 失败可见：飞书或日志能看到。
- 失败可恢复：可重新登录、重试失败、重跑摘要。
- 失败不污染数据：解析失败不写 Markdown；写入失败不标记成功。
- 失败不扩大影响：低频串行访问，单篇失败不影响其他条目。

## 7. 最终策略置信判断

在“Phase 1 只做 Windows 本地文字稿同步”的范围内，修正后的策略达到事实上的高置信。

我不会把它表述为数学意义的 100%。更准确的判断是：

- 已知 P0 风险：全部有硬性边界或阻断机制。
- 已知 P1 风险：全部有检测、降级或恢复方案。
- 未知网页变化风险：无法消除，但已被隔离为 extractor 失败，不会静默污染笔记。
- MVP 不依赖 Obscura、转录、Debian、媒体下载这些不确定项。

因此，下一步可以安全进入 Phase 1 项目骨架开发，但必须把 preflight、状态机、幂等写入、日志脱敏和 extractor 质量门槛作为第一批实现内容。

## 8. 需要同步回技术设计的变更

- 增加 preflight 模块。
- 增加 extractor 质量门槛。
- 增加 `extractor_failed` 状态。
- Markdown frontmatter 使用内置 YAML-safe scalar renderer。
- 文件写入使用临时文件 + 原子移动。
- 飞书通知默认不发送全文。
- 明确 Phase 1 不接入转录。
- Debian 迁移前要求 Windows MVP 连续稳定运行 7 天。

## 9. 第四轮：实现后事实核验

截至 2026-05-27，已完成一轮“策略漏洞 -> 修复措施 -> 测试核验”的实现后审查：

| 漏洞 | 修复状态 | 事实核验 |
| --- | --- | --- |
| 页面解析失败只能人工看日志，难以批量比较四个栏目快照 | 已为 `parse-snapshot` 增加 `--json` 机器可读报告，包含正文质量、候选正文、候选条目和输出路径 | 新增 CLI 测试覆盖 JSON 输出；真实快照保存后可固化为回归基线 |
| 自动保存 HTML 可能泄露会员内容 | `save_failure_html` 默认关闭；仅调试时保存失败详情页 HTML 到 `.gitignore` 覆盖的 `data/page_failures/`，并在失败记录中写入路径 | 配置默认值、保存路径和 sync 失败记录均有单元测试 |
| 空登录态文件被误判为可用 | `doctor`、`preflight` 和 `login` 保存后校验 Playwright `storage_state` JSON 结构，并要求 cookies/origins 至少一项非空 | 单元测试覆盖有效、坏结构和空 cookies/origins |
| 配置错误可能到运行时才暴露 | `doctor` 和 `preflight` 已检查栏目、URL、文件命名模板、vault、依赖和环境变量 | `doctor --config config.example.yaml --no-auth --json` 返回 `config_semantics=ok`；`preflight --no-auth --no-browser` 通过 |
| 文件名模板包含未知字段导致写入时才崩溃 | `filename_pattern` 只允许 `{column}`、`{published_date}`、`{title}`，未知字段在 preflight/doctor 阶段报错 | Preflight 和 doctor 单元测试覆盖 `{dedao_id}` 等未知字段 |
| 文字稿解析误写风险 | extractor 已加入长度、段落、UI 噪声和标题相关性门槛；失败标记为 `extractor_failed` | 单元测试覆盖正文提取、候选诊断和离线快照解析 |
| 每日运行可能“半失败但显示成功” | run 状态只在失败、无文字稿、摘要失败计数全部为 0 时为 `success`，否则为 `partial_failed` | 单元测试覆盖运行状态、失败列表和 run item 明细 |
| 摘要接口返回异常格式可能假成功 | 摘要解析要求 JSON 或可识别 Markdown 章节，空/无效返回抛 `SummaryError` 并标记 `summary_failed` | 单元测试覆盖空 JSON、非结构化文本、摘要失败不中断全文保存 |
| 摘要 API 请求格式漂移或错误体泄露密钥 | OpenAI-compatible adapter 固定 `/chat/completions` 请求、model、Authorization 和 JSON prompt；HTTP 错误体进入 `SummaryError` 前统一脱敏 | Summarizer 单元测试覆盖请求 payload、响应解析和 HTTP 错误脱敏 |
| 飞书通知失败可能打印 traceback 或泄露全文 | `notify-test` 捕获异常；通知只发摘要、标题、失败和日志路径；HTTP 错误体进入异常前统一脱敏；预检查通知失败也写 warning 日志 | 单元测试覆盖通知异常、HTTP 错误体脱敏、`run_preflight` 通知失败日志；真实 webhook 因当前网络限制尚未连通 |
| 状态库或 CLI 输出泄露 token/cookie | 已在日志、SQLite 错误字段、run item 明细、CLI 列表/失败输出边界统一脱敏 | 单元测试覆盖 `Authorization`、Cookie、API key、secret 和飞书 webhook |
| 手动演练命令误发飞书日报 | `check` 和 `sync --dry-run` 均显式关闭通知发送 | CLI 和 sync 单元测试覆盖 `send_notification=False` |
| 飞书标题泄露或关闭开关失效 | 增加 `feishu.include_titles` 隐藏标题；`feishu.enabled=false` 时即使存在 webhook 环境变量也不发送；正式 `sync`、`retry-failed`、`resummarize` 在启用飞书但缺 webhook 时预检查失败；`check`/`sync --dry-run` 不强制要求 webhook | 配置、通知、preflight 和 sync 单元测试覆盖 |
| YAML 字符串布尔值误解析 | 增加严格布尔解析，`"false"`、`"0"`、`"off"` 等不会被 Python 当作 truthy 字符串；拼写错误如 `"flase"` 会直接报配置错误 | 配置单元测试覆盖 quoted false 和非法布尔字符串 |
| 无文字稿内容缺少后续转录线索 | 详情页解析会记录网页正常暴露的媒体候选；MVP 不下载媒体，只在 `missing_transcript` 失败说明和快照 JSON 中显示候选数量/类型 | extractor、CLI JSON 和 sync 单元测试覆盖 |
| 任务计划早期失败无项目日志 | Windows wrapper 写 `logs/scheduled-YYYY-MM-DD.log`，记录 PowerShell 层面的启动、参数和退出码 | 脚本静态测试覆盖 transcript、配置参数和任务注册参数 |
| Windows 定时维护任务需要改脚本才能传参数 | wrapper 和任务注册脚本支持 `-Command` 与剩余参数透传，默认每日 `sync` 不变，同时可注册指定栏目或 `resummarize --all` 等维护任务 | 脚本静态测试覆盖额外参数透传 |
| Windows wrapper 多个额外参数被合成一个参数 | 透传参数改为 wrapper 已知参数后的剩余参数，`ExtraArgs` 仅作内部捕获；文档示例直接写 `--column "栏目"` / `--all` | wrapper 实际演练覆盖 `check --column __NO_SUCH_COLUMN__` |
| `$PSScriptRoot` 在参数默认值中为空 | Windows bootstrap、wrapper 和注册脚本把 `ProjectRoot` 默认值改为空字符串，在脚本 body 内用 `$PSScriptRoot` 推导项目根目录 | wrapper 实际演练覆盖默认 ProjectRoot；脚本测试覆盖 bootstrap |
| Windows 任务注册保存相对项目路径 | 注册时把 `ProjectRoot` 和 wrapper 脚本路径解析为绝对路径，避免任务计划程序在不同工作目录下运行时找错项目 | 脚本静态测试覆盖绝对路径解析 |
| 摘要 prompt/模型更新后无法刷新已成功笔记 | `resummarize` 默认只补缺失/失败摘要；新增显式 `resummarize --all` 刷新所有已有全文稿笔记 | CLI 和 sync 单元测试覆盖默认跳过与 `--all` 刷新 |
| Debian 迁移缺少可操作部署骨架 | 增加 systemd user service/timer 模板和 `docs/DEBIAN_DEPLOY.md`，明确仍需 Windows MVP 7 天稳定后迁移 | 静态测试覆盖 systemd 模板关键字段和文档链接 |
| 来源链接只在 frontmatter 中，不方便 Obsidian 阅读时回到原文 | Markdown 正文增加 `## 来源` 段，并放在 `## 全文稿` 之前，避免污染重摘要的全文抽取 | Markdown 单元测试覆盖正文来源段和全文稿抽取边界 |
| 同名文件短 hash 路径再次撞名会覆盖已有笔记 | Markdown writer 在原文件和短 hash 文件都存在时继续追加序号，保留所有已有文件内容 | Markdown 单元测试覆盖原文件、短 hash 文件和新写入文件三者并存 |
| SQLite schema 演进后旧库无法升级 | `migrate()` 增加向前兼容字段补齐与最小回填，覆盖 `canonical_url` 等后加字段 | Repository 单元测试用旧版 `items`/`runs`/`run_items` 表验证升级后仍可 canonical URL 去重 |
| 栏目页链接文本为空或只是短按钮导致漏识别内容 | `_page_anchors` 和离线 snapshot 解析都采集 `title`、`aria-label`、`data-title` 和父卡片文本，`items_from_anchors` 优先选择长度足够的标题候选 | Snapshot 单元测试覆盖短链接文本回退到无障碍标题、卡片文本，以及 `parse-snapshot` 离线候选识别 |
| Playwright 包已安装但 Chromium 未安装，预检查误通过 | `preflight` 和 `doctor` 增加 `dep:playwright_chromium` 检查，验证 Chromium executable 是否存在 | Browser、doctor 和 preflight 单元测试覆盖缺包/缺浏览器诊断 |
| 登录态运行时失效但没有通知 | `crawler.check_login()` 失败时状态为 `login_required`，写入 run 错误并继续发送飞书运行报告 | Sync 单元测试覆盖登录失效、run 记录和通知触发 |
| 敏感运行文件规则被后续改动误删 | `.gitignore` 覆盖 `.env`、`config.yaml`、登录态、浏览器 profile、数据库、日志、页面快照和媒体缓存 | Gitignore 静态单元测试固定这些敏感路径规则 |
| 飞书明细截断后隐藏剩余问题规模 | 每类通知明细保留前 10 条，同时显示剩余数量并提示查看日志或 `list` 命令 | Notifier 单元测试覆盖新增内容和失败明细的截断计数 |
| 固定请求间隔形成机械访问节奏 | Crawler 在基础 `request_interval_seconds` 上增加小幅随机抖动，且保持串行访问 | Crawler 单元测试覆盖正数基础间隔加抖动、0 间隔不调用随机数 |
| 详情页真实标题/作者/发布时间只在 JSON-LD 中导致元数据缺失 | Metadata extractor 增加 JSON-LD `headline/name/author/datePublished` 解析 | Extractor 单元测试覆盖 JSON-LD 标题、多人作者和发布时间 |
| 本地执行历史看不出具体失败类型 | `list --runs` 输出补齐 `skipped`、`missing` 和 `summary_failed` 计数，飞书遗漏时也能快速判断下一步动作 | CLI 单元测试覆盖运行历史输出字段 |
| 合规边界只停留在文档 | 详情页解析后执行 `policy` guard，遇到 DRM、Encrypted Media Extensions、加密 HLS key 或 DASH manifest 记为 `policy_blocked`，不写笔记、不自动重试，并进入 `list --failed` | Policy、extractor、sync 和 CLI 单元测试覆盖 |
| 无法观察真实网页访问频率 | `RunReport` 和 SQLite `runs` 增加 `request_count`，登录检查、栏目列表和详情页访问都会计数，并在飞书与 `list --runs` 中显示 | Repository、sync、notifier 和 CLI 单元测试覆盖 |
| Windows 与 Debian 主机时区不同导致日报和笔记同步时间错位 | 新增统一 `now_local()`，面向用户的运行报告、飞书通知、Markdown `sync_time`、日志日期和失败快照时间戳固定为 `Asia/Shanghai`；缺少 IANA `tzdata` 时回退到固定 UTC+08；数据库内部条目审计时间仍保留 UTC | Time utils、Markdown 和 notifier 单元测试覆盖 |
| 受限环境无法安装 PyYAML 导致 doctor 长期告警 | PyYAML 从核心依赖降为可选增强；项目保留内置有限 YAML 解析器覆盖当前配置模板；doctor 对缺少 PyYAML 显示 ok 说明而非 warn | Config 和 doctor 单元测试覆盖 |
| 标题或摘要短字段含换行时打乱 Markdown 结构 | Markdown renderer 对标题、来源、关联、行动、复习问题、关键词等短字段统一折叠为单行；全文稿仍保留原段落；关联段同步纳入“可延伸问题” | Markdown 单元测试覆盖结构注入和关联问题呈现 |
| 摘要 base URL 或飞书 webhook 拼错到运行时才暴露 | `preflight` 和 `doctor` 增加 `http(s)` URL 格式检查；摘要 URL 错误给 warning，正式飞书 webhook 错误在 require_feishu 场景下作为 error | Preflight 和 doctor 单元测试覆盖 |
| Markdown 已写入但 DB 更新前中断后重跑生成重复笔记 | Markdown writer 在目标文件已存在且内容一致时直接复用原文件；比较笔记身份时忽略动态 `sync_time`；只有内容不同才追加短 hash 或序号 | Markdown 单元测试覆盖 DB 中断恢复和 `sync_time` 变化场景 |

本轮命令核验：

```text
.venv\Scripts\python.exe -B -m unittest discover -s tests -> 144 tests OK
py -B -m unittest discover -s tests                         -> 144 tests OK
ast syntax scan                     -> 39 files OK
PowerShell parser scan              -> bootstrap/run/register scripts OK
run_dedao_sync.ps1 -Command check --column __NO_SUCH_COLUMN__ -> expected preflight_failed
doctor --config config.example.yaml --no-auth --json
preflight --config config.example.yaml --no-auth --no-browser
```

当前不能声称端到端事实 100% 的剩余外部条件：

- Playwright 依赖未安装，`doctor` 仍报告 `dep:playwright=warn`。
- 当前终端运行依赖安装时，提权路径被 Windows OS 740 拦截，普通路径被网络权限 `WinError 10013` 拦截，尚未完成 Playwright 安装。
- 得到网页登录态尚未保存，`doctor` 仍报告 `auth_state=warn`。
- 四个真实栏目页面还没有登录后快照，因此真实 DOM 选择器尚未完成校准。

因此，当前策略的准确表述是：

- 本地状态机、幂等写入、错误可见性、摘要解析、通知异常处理、离线页面解析诊断这些可离线验证的部分，已经达到事实高置信。
- 真实网页访问、登录态、外部摘要、飞书通知这四条外部链路，必须在允许网络和 Playwright 的运行环境中完成实测后，才能进入“事实上的端到端高置信”。
