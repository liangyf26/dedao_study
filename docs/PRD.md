# 得到内容同步到 Obsidian - PRD v0.2

最后更新：2026-05-26

## 1. 背景

用户已购买得到 App 会员，并长期收听若干固定栏目。得到网页版上有部分栏目内容提供文字稿。用户希望用一个本地 Python 项目，自动检查固定栏目的每日更新，将可访问的文字稿或转录稿整理为 Obsidian Markdown 笔记，便于长期保存、检索和复习。

本项目仅面向个人学习资料归档，不用于内容传播、破解、绕过访问限制或批量分发。

## 2. 产品目标

- 每天自动检查指定得到栏目是否有新内容。
- 对已有网页文字稿的内容，保存完整正文到 Obsidian。
- 对没有网页文字稿但有可合法访问音频/视频的内容，后续支持转录为文字。
- 为每篇内容生成卡片笔记/Zettelkasten 风格提要。
- 每次执行后，将同步结果发送到飞书。
- 保存登录态，减少人工登录频率。
- 避免重复下载，记录同步状态和失败原因。

## 3. 首批栏目

| 栏目 | URL |
| --- | --- |
| 快刀青衣·快刀广播站 | https://aiquan.dedao.cn/courseList?type=1 |
| 尹烨·健康参考 | https://www.dedao.cn/course/detail?id=zp9lB3q0breKZq4sDWXYjyWxG64dg2 |
| 马江博·政经参考 | https://www.dedao.cn/course/detail?id=ZWyMAOLnR4xJ1vqse8X65QaE8YG29k |
| 脱不花·长谈 | https://www.dedao.cn/course/detail?id=ElLD8OrepAxVvGMs4kJ2oybGdmBnvM |

## 4. 运行环境策略

### MVP 阶段

优先在 Windows 11 + Codex App 所在机器运行。

原因：

- 便于首次网页登录、扫码、验证码、人机校验等人工交互。
- 便于观察网页结构和调试浏览器自动化。
- Obsidian vault 已位于 Windows 本地路径：`D:\biji\openclaw-vault`，本项目的内容放在： `D:\biji\openclaw-vault\5-收件箱(Inbox)\得到\` 下。
- 早期任务以网页文字稿同步为主，对 24 小时常驻要求不高。

### 稳定阶段

后续迁移到 Debian + Codex CLI 常驻运行。

迁移前提：

- 登录态可以迁移，或能在 Debian 环境完成一次可维护的登录流程。
- Obsidian vault 有稳定同步方式，例如 Syncthing、SMB、Git 或其他文件同步方案。
- 转录依赖在 Debian 上部署稳定。

## 5. 功能范围

### 5.1 包含

- 配置固定栏目。
- 手动登录并保存登录态。
- 每天自动检查栏目更新。
- 提取内容详情页元数据和网页文字稿。
- 生成 Obsidian Markdown 文件。
- 生成卡片笔记/Zettelkasten 风格提要。
- 使用 SQLite 记录同步状态。
- 通过飞书自定义机器人发送每日执行结果。
- 对无文字稿内容记录状态，为后续转录流程预留接口。

### 5.2 暂不包含

- 破解会员、验证码、DRM、加密媒体或风控机制。
- 抓取用户无权访问的内容。
- 对外分享、发布、转售或批量传播内容。
- 移动 App 逆向。
- 多用户账号系统。
- 云端托管后台。

## 6. 核心用户故事

- 作为用户，我可以配置几个固定栏目，系统每天自动检查更新。
- 作为用户，我只需要手动登录一次，后续系统尽量复用登录状态。
- 作为用户，我希望每篇内容保存为 Markdown 文件，并能直接在 Obsidian 中打开。
- 作为用户，我希望笔记包含标题、栏目、发布日期、原始链接、卡片笔记、永久笔记、关联问题和全文稿。
- 作为用户，当某篇内容没有网页文字稿时，我希望系统标记出来，后续可尝试转录。
- 作为用户，我希望每天执行完成后收到飞书通知，知道新增、跳过、失败数量。

## 7. 功能需求

### 7.1 配置管理

配置文件建议为 `config.yaml`，包含：

- Obsidian vault 路径：`D:\biji\openclaw-vault\5-收件箱(Inbox)`
- 输出子目录：`得到`
- 栏目列表
- 每日运行时间
- 摘要模型配置
- 转录开关和转录引擎
- 飞书通知配置
- 文件命名规则
- 是否保留 HTML 或媒体缓存

### 7.2 登录管理

- 首次运行 `dedao-sync login` 时打开浏览器，用户手动登录得到网页版。
- 登录完成后保存浏览器登录态。
- 后续运行自动加载登录态。
- 登录态失效时提示重新登录，并在飞书通知中标记失败原因。
- 不保存明文账号密码。
- 登录态文件视为敏感文件，不进入 Git。

### 7.3 栏目更新检测

- 访问配置中的栏目页面。
- 解析内容列表，获得文章/音频/视频条目。
- 与 SQLite 中已同步记录比对，识别新内容。
- 支持仅检查不下载。

### 7.4 内容同步

对每篇新内容：

- 打开详情页。
- 读取标题、栏目、发布日期、作者/讲者、原始 URL。
- 优先提取网页可见文字稿。
- 没有文字稿时记录 `missing_transcript`。
- 成功后写入 Obsidian，并记录 SQLite 状态。

### 7.5 摘要生成

- 摘要模型：DeepSeek v4 Pro，经 OpenCode GO 订阅提供的 API Key 调用。
- 设计上按 OpenAI-compatible API 抽象；如果 OpenCode GO 的接口不兼容，再实现专用 adapter。
- API Key 放入 `.env`，不写入 Git。
- 摘要失败不阻断全文保存，状态记录为 `summary_failed`。

摘要风格：

- 卡片笔记/Zettelkasten。
- 输出原子卡片、永久笔记、关联、行动/观察、复习问题。
- 语言默认中文。

### 7.6 无文字稿转录

MVP 阶段只记录无文字稿状态，不强制实现转录。

后续阶段：

- 检查页面是否有用户可正常播放、可合法访问的音频/视频。
- 不绕过 DRM 或加密保护。
- 临时下载媒体文件用于转录。
- 调用本地 ASR 引擎生成文字稿。
- 转录完成后删除原始媒体缓存。
- Markdown 中标注“本文稿由音频/视频自动转录生成，可能存在识别错误”。

### 7.7 Obsidian 输出

输出根目录：

```text
D:\biji\openclaw-vault\5-收件箱(Inbox)\得到\
```

文件结构：

```text
得到/
  栏目名/
    栏目-日期-标题.md
```

文件名格式：

```text
栏目-YYYY-MM-DD-标题.md
```

标题中的非法文件名字符需要替换或删除。

### 7.8 飞书通知

每天执行完成后发送飞书通知。

通知内容包括：

- 执行时间
- 运行机器
- 总栏目数
- 新增文章数
- 跳过文章数
- 失败文章数
- 无文字稿文章数
- 摘要失败数
- 每个栏目新增标题列表
- 失败原因摘要
- 本地日志路径

通知方式：

- 使用飞书自定义机器人 webhook。
- 支持签名密钥。
- webhook 和 secret 存放在 `.env`。
- 通知失败不影响同步主流程，但需要写入日志。

## 8. Markdown 模板

```markdown
---
source: dedao
column: 栏目名
title: 标题
author: 作者
published: 2026-05-26
url: 原文链接
content_type: transcript
summary_style: zettelkasten
sync_time: 2026-05-26 08:00:00
tags:
  - 得到
  - 栏目名
---

# 标题

## 原子卡片

### 卡片 1：核心概念

用自己的话重述一个独立知识点。

### 卡片 2：关键判断

解释作者的判断、依据和适用边界。

## 永久笔记

将本文最值得长期保存的观点整理成自己的表达。

## 关联

- 可关联主题：
- 可延伸问题：
- 与已有知识的冲突或补充：

## 行动/观察

- 可以尝试的行动：
- 后续值得观察的信号：

## 全文稿

正文内容。
```

## 9. 数据存储

使用 SQLite 记录同步状态。

核心表：`items`

- `id`
- `source_url`
- `dedao_id`
- `column_name`
- `title`
- `published_at`
- `synced_at`
- `content_hash`
- `status`
- `file_path`
- `has_transcript`
- `transcribed`
- `summary_status`
- `error_message`

用途：

- 避免重复同步。
- 支持失败重试。
- 支持后续补摘要、补转录。
- 便于排查页面结构变化。

## 10. 命令行需求

```bash
dedao-sync login
dedao-sync check
dedao-sync sync
dedao-sync sync --column 栏目名
dedao-sync retry-failed
dedao-sync notify-test
dedao-sync list
```

## 11. 自动化运行

MVP：

- Windows 任务计划程序每天运行一次。
- 默认时间建议：Asia/Shanghai 08:00。

稳定阶段：

- Debian 使用 systemd timer 或 cron。

## 12. 合规与安全要求

- 只同步用户本人有权访问的内容。
- 不绕过登录、会员限制、验证码、风控、DRM 或加密保护。
- 不提供外传、公开分享、重新发布功能。
- 不保存明文密码。
- `.env`、登录态、cookie、数据库和日志中的敏感信息不进入 Git。
- 请求频率保持克制，默认低并发或串行。
- 页面结构变化导致异常时，应停止相关任务并提示人工检查。

## 13. 验收标准

- 首次登录后，系统能保存登录态。
- 配置四个首批栏目后，系统能检查并识别新内容。
- 有网页文字稿的内容能生成完整 Markdown 文件。
- 重复运行不会重复创建文件。
- 每篇笔记包含 frontmatter、卡片笔记、永久笔记、关联、行动/观察、全文稿和来源链接。
- 每次执行后能向飞书发送结果通知。
- 登录失效、页面变化、摘要失败、无文字稿等情况有明确日志和状态记录。

## 14. 后续待确认

- OpenCode GO 的 DeepSeek v4 Pro API endpoint、模型名和兼容格式。
- 飞书机器人 webhook 和是否开启签名。
- Windows 本地是否安装 GPU，影响后续转录方案。
- Debian 与 Obsidian vault 的同步方式。
- 是否需要将每日通知同时生成一篇运行日志笔记。

