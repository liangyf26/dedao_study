# Debian 常驻部署

最后更新：2026-05-28

这是 Phase 4 的准备文档。当前建议先让 Windows MVP 连续稳定运行 7 天，再迁移到 Debian。

## 前置条件

- Debian 主机已安装 Python 3.11+。
- 项目放在用户家目录下，例如 `~/dedao-sync`。
- Obsidian vault 已通过 Syncthing、云盘或其他方式同步到 Debian 可访问路径。
- 已在 Debian 上配置 `config.yaml` 和 `.env`。
- 已完成登录态方案：要么在 Debian 本机运行 `dedao-sync login`，要么迁移并验证 Playwright `storage_state`。
- 已安装 Playwright 浏览器依赖，并能通过 `dedao-sync doctor --config config.yaml`。

## 安装依赖

```bash
cd ~/dedao-sync
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m playwright install chromium
```

如需在无桌面环境中完成网页登录，优先使用可维护的 Xvfb/headful 方案，避免绕过验证码或风控。

## 首次验证

```bash
cd ~/dedao-sync
.venv/bin/dedao-sync doctor --config config.yaml
.venv/bin/dedao-sync check --config config.yaml
.venv/bin/dedao-sync sync --config config.yaml --dry-run
```

`check` 和 `sync --dry-run` 不发送飞书通知，也不要求 webhook 环境变量存在。正式 `sync`、`retry-failed` 和 `resummarize` 在 `feishu.enabled: true` 时会要求 webhook 环境变量存在。

## 安装 systemd user timer

模板位于：

```text
templates/systemd/dedao-sync.service
templates/systemd/dedao-sync.timer
```

默认模板假设项目路径是 `~/dedao-sync`。如果实际路径不同，请先复制模板并修改 `WorkingDirectory`、`EnvironmentFile` 和 `ExecStart`。

```bash
mkdir -p ~/.config/systemd/user
cp templates/systemd/dedao-sync.service ~/.config/systemd/user/
cp templates/systemd/dedao-sync.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dedao-sync.timer
```

允许用户服务在退出登录后继续运行：

```bash
loginctl enable-linger "$USER"
```

## 查看状态和日志

```bash
systemctl --user list-timers dedao-sync.timer
systemctl --user status dedao-sync.service
journalctl --user -u dedao-sync.service -n 100 --no-pager
```

项目内部日志仍会写入：

```text
logs/
```

## 手动运行和恢复

```bash
systemctl --user start dedao-sync.service
.venv/bin/dedao-sync list --config config.yaml --runs
.venv/bin/dedao-sync list --config config.yaml --failed
.venv/bin/dedao-sync retry-failed --config config.yaml
.venv/bin/dedao-sync resummarize --config config.yaml
```

`sync`、`retry-failed` 和 `resummarize` 共用项目锁 `data/dedao_sync.lock`，避免 systemd timer 与手动命令并发写入。

## 暂停或卸载

```bash
systemctl --user disable --now dedao-sync.timer
systemctl --user stop dedao-sync.service
```

## 迁移检查清单

- Windows MVP 已连续稳定运行 7 天。
- Debian 上 `doctor` 没有 error。
- 登录态可用，且失效时能重新登录。
- vault 路径可写，并已确认同步方式不会和 Obsidian 冲突。
- `notify-test` 能成功发送飞书通知。
- `summary-test` 能成功调用摘要模型。
