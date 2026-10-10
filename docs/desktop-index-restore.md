# 从云端恢复 Claude Code 会话到桌面 App：排查记录与改进方案

日期：2026-10-09　机器：majiajiaodeMac-mini（macOS，桌面 App 为 Claude-3p 版本）

## 一、目标

把 aisync 备份在 Google Drive（restic 仓库 `rclone:gdrive:aisync`）里的 Claude Code 会话恢复到本机，并且能在桌面 App 的 Code 侧栏里看到。Codex 不在本次范围。

## 二、处理过程

| 步骤 | 现象 | 原因 | 处理 |
|---|---|---|---|
| 1. 只导入元数据 | 有 2598 条会话索引，但没有正文 | 正文 JSONL 只存在 restic 快照里，git 仓库 `~/aisync/repo` 只有索引和小文件 | 改为从 restic 恢复 |
| 2. `cmd_restore` 恢复 | `exec: "rclone": executable file not found in $PATH` | rclone/restic 是 aisync 自带的二进制，放在 `~/aisync/bundle/bin/`，终端 PATH 里没有 | `export PATH="$HOME/aisync/bundle/bin:$PATH"` |
| 3. 再次恢复 | `error talking HTTP to rclone: exit status 1` | Google Drive OAuth token 已失效（access token 9 月 14 日过期，refresh 也失败）。restic 把 rclone 的真实报错吞掉了 | `rclone lsd gdrive:aisync` 看真实报错，`rclone config reconnect gdrive:` 重新授权 |
| 4. 恢复成功 | 快照里只有 14 个会话，147 MB | 当初备份时 UI 只勾选了这 14 个（见 `cache/backup-paths.txt`），其余约 2580 个从没上传 | 无法从云端补回，只能从本机 `~/.claude/projects` 取 |
| 5. 导入侧栏 | Code 标签页仍为空 | 见下面第三节 | 临时脚本 `~/aisync/import_to_app.sh` |

恢复时没有直接用 `cmd_restore`，而是手动执行 `restic restore latest --target ~/aisync/restored/claude-only --include ~/.claude/projects`。这样不会覆盖本机已有的会话，也不会去碰 Codex 的索引。

## 三、侧栏为什么是空的

桌面 App 的 Code 侧栏不扫描 `~/.claude/projects`，它读的是 App 自己的索引：

```
~/Library/Application Support/<变体>/claude-code-sessions/<accountUuid>/<profileUuid>/local_<uuid>.json
```

每条索引通过 `cliSessionId` 指向 `~/.claude/projects/<编码目录>/<cliSessionId>.jsonl`。

aisync 里 `DESKTOP_DIRS["darwin"]` 写死为 `~/Library/Application Support/Claude`。这台机器装的是 **Claude-3p** 变体，数据在 `~/Library/Application Support/Claude-3p`，所以：

- 导出时：`export_desktop_index` 只读 `Claude`。如果用户只用 3p 版，`repo/desktop-index` 里一条都没有（这次能导出 14 条，是因为以前用过官方版）。
- 导入时：`import_desktop_index` 只写 `Claude`。3p 版的侧栏永远看不到恢复的会话。
- 另外，如果 `cmd_restore` 没先把 JSONL 放回 `~/.claude/projects`，`import_desktop_index` 会因为 `cliSessionId not in have` 跳过所有条目，不会生成孤儿索引。所以第 4 步必须真正落到原位，而不是只停在 `restored/` 里。

## 四、aisync 代码改进方案

改动集中在 `core.py` 第 410–504 行的"桌面版会话索引"一节，外加 UI 文案和 CLI 入口。

### 1. 支持多个桌面版变体

```python
DESKTOP_NAME = re.compile(r"^Claude(-[A-Za-z0-9._]+)?$", re.I)   # Claude、Claude-3p…；不匹配 "Claude Code"

def _desktop_parent():
    if IS_WIN: return Path(os.environ.get("APPDATA") or HOME / "AppData/Roaming")
    if IS_MAC: return HOME / "Library/Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")

def desktop_bases():
    """本机所有 Claude 桌面版数据目录，官方版排第一；AISYNC_DESKTOP_DIR 可手动指定（os.pathsep 分隔）"""
    env = os.environ.get("AISYNC_DESKTOP_DIR")
    if env: return [Path(p) for p in env.split(os.pathsep) if p and Path(p).is_dir()]
    try: ds = [d for d in _desktop_parent().iterdir() if d.is_dir() and DESKTOP_NAME.match(d.name)]
    except OSError: return []
    return sorted(ds, key=lambda d: (d.name.lower() != "claude", d.name.lower()))
```

`desktop_installed()` 改为 `bool(desktop_bases())`。`desktop_index_dir(base, create=False)` 增加 `base` 参数，`root = base / "claude-code-sessions"`，`ant-device-registry.json` 也从 `base` 下取。其余逻辑不变。

### 2. 导出：合并所有变体

`export_desktop_index` 遍历 `desktop_bases()`。同一个 `local_*.json` 在多个变体里都有时，保留 `lastActivityAt` 最大的那条。日志按变体分别输出条数。

### 3. 导入：每个变体都写

`import_desktop_index` 对每个 base 调用 `desktop_index_dir(base, create=True)`，逐个写入，安全规则保持原样：只写本机已有 JSONL 的条目，不覆盖同名文件，按 `path_map` 改写 cwd。返回总写入数，日志写成"Claude-3p：写入 14 条"这种形式。

### 4. 新增"只重建侧栏索引"命令（不联网）

适用场景是：会话 JSONL 已经在 `~/.claude/projects`，但侧栏没有显示，比如这次的情况，或者用户手动拷过 JSONL。

- `core.cmd_reindex(logger=log)`：调用 `import_desktop_index(REPO/"desktop-index")`。
- 可选增强：对 `repo/desktop-index` 里没有对应条目的 JSONL，根据 JSONL 自己生成索引。字段来源：`cliSessionId` 取文件名，`cwd` 取首条带 cwd 的记录，`title` 取 `custom-title` > `ai-title` > 首条用户消息前 50 字，`createdAt`/`lastActivityAt` 取首末 timestamp 转毫秒，`sessionId` 用 `"local_" + uuid4()`，`isArchived=False`。只对 `entrypoint == "claude-desktop"` 的会话生成，CLI 会话保持原样。
- CLI：在 `main()` 里加 `elif c == "reindex": cmd_reindex()`，同时更新文件头 docstring 的用法。
- UI：`ui.py` 加 `/api/reindex`（照抄 `/api/repair` 的写法）；`ui.html` 在"修复会话不显示"卡片里加一个"重建桌面版侧栏索引"按钮，并删掉那句"修复后请用 `claude --resume` 查看"。

### 5. 其他顺手修复

- **rclone 报错被吞**：`cmd_restore`/`cmd_snapshot` 失败时，如果仓库以 `rclone:` 开头，先跑一次 `rclone lsd <remote>:` 把真实报错写进日志；遇到 `invalid_grant` / `token` 字样时，提示"Google Drive 授权失效，请在设置里重新授权"。
- **CLI 找不到自带二进制**：`tool_env()` 里把 `AISYNC/"bundle"/"bin"`（以及 .app 内的 `Contents/Frameworks/res/bin`）存在时插到 PATH 前面，命令行和桌面版行为就一致了。`which()` 也要基于同一个 PATH。
- **备份覆盖率**：push 时在日志里打印"已备份 N / 本机共 M 个会话"，避免这次"以为备份了 2598 个，其实只有 14 个"的情况。

### 6. 测试要点

- 用临时 HOME 模拟 `Library/Application Support/{Claude,Claude-3p,Claude Code}`：`desktop_bases()` 只返回前两个，`Claude` 排第一。
- 导出时两个变体有同名条目，保留 `lastActivityAt` 较新的那条。
- 导入：JSONL 不存在时跳过；目标已存在时不覆盖；两个变体各写一份；`AISYNC_DESKTOP_DIR` 覆盖探测结果。
- `reindex` 连续跑两次，第二次写入 0 条（幂等）。
- Windows 路径（`%APPDATA%\Claude-3p`）用 `IS_WIN` 分支覆盖。

## 五、相关文件

- 恢复出的会话：`~/aisync/restored/claude-only/Users/majiajue/.claude/projects/`（14 个，未脱敏原件）
- 脱敏导出：Cowork 输出目录 `claude-import/claude-sessions/`（jsonl + 可读 md + index.json）
- 临时导入脚本：`~/aisync/import_to_app.sh`。功能做进 aisync 后可以删掉，它不在 git 里。
- 侧栏索引来源：`~/aisync/repo/desktop-index/local_*.json`（14 条，与快照里的会话一一对应）

## 六、待办

- 运行 `bash ~/aisync/import_to_app.sh`，确认 Claude-3p 侧栏能显示这 14 个会话。如果不行，它的第 2 步输出能看出 3p 版实际用的目录结构，按结果调整上面的方案。
- 轮换会话里出现过的明文密钥（sk-、GitHub、Google API key、Longbridge auth code 等）。
- 如果 Google Cloud 里的 OAuth 应用还处于"测试"状态，发布为 In production，避免 refresh token 7 天后失效。
