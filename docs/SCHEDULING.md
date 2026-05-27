# Windows 定时运行

最后更新：2026-05-27

## 前置条件

- `config.yaml` 已填写。
- `.env` 已填写飞书 webhook 和摘要 API 配置。
- 已执行 `dedao-sync login --config config.yaml` 并保存登录态。
- `dedao-sync doctor --config config.yaml` 没有 `ERROR`。
- `dedao-sync check --config config.yaml` 可以正常完成。

机器可读诊断：

```powershell
dedao-sync doctor --config config.yaml --json
```

## 手动运行一次

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\run_dedao_sync.ps1
```

## 注册 Windows 任务计划

默认每天 08:00 执行：

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\register_windows_task.ps1
```

指定时间：

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\register_windows_task.ps1 -At "07:30"
```

任务名默认为：

```text
DedaoSyncToObsidian
```

## 查看任务

```powershell
Get-ScheduledTask -TaskName DedaoSyncToObsidian
```

## 手动触发任务

```powershell
Start-ScheduledTask -TaskName DedaoSyncToObsidian
```

## 删除任务

```powershell
Unregister-ScheduledTask -TaskName DedaoSyncToObsidian -Confirm:$false
```

## 日志

同步日志写入：

```text
logs/
```

每日运行结束后，无论成功、部分失败还是登录失效，都会尽量通过飞书发送运行结果。飞书发送失败不会影响主流程，但会写入日志。

## 重叠运行保护

项目内部会使用：

```text
data/dedao_sync.lock
```

作为运行锁，避免 `sync`、`retry-failed`、`resummarize` 同时执行。

如果任务计划、手动命令或异常重试发生重叠，后启动的任务会返回 `locked` 状态并退出。超过 6 小时的陈旧锁会被自动清理并接管。

Windows 任务计划脚本也设置了 `IgnoreNew`，尽量避免任务计划本身并行启动。项目内部锁是第二层保护。
