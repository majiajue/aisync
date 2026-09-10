<p align="center"><img src="icon-preview.png" width="96"></p>

# aisync

**像换手机一样迁移 Claude Code 和 Codex：会话、技能、记忆、配置、项目代码，一键上云，新电脑一键接回。**

Sync and migrate your Claude Code & Codex sessions, skills, memories, settings and project code across machines — like restoring a new phone from backup. Local-first, end-to-end encrypted, works with Google Drive / Cloudflare R2 / OneDrive / WebDAV / S3 / local disks. macOS · Windows · Linux.

[![build](https://github.com/majiajue/aisync/actions/workflows/build.yml/badge.svg)](https://github.com/majiajue/aisync/actions) [![license](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE) [![release](https://img.shields.io/github/v/release/majiajue/aisync)](https://github.com/majiajue/aisync/releases)

## 为什么

`~/.claude` 和 `~/.codex` 里攒了几千个会话、几十个技能和记忆，换电脑或者多台机器切换时全丢了。aisync 把它们分两层管起来：小而重要的走 git 持续同步，大而只增的走 restic 加密快照存到任意网盘；桌面版内置 restic 和 rclone，图形界面里勾选会话和代码目录即可，新电脑只需要 git 地址加一个密码。

## 特性

- 会话按项目分组浏览、搜索、按时间筛选，勾会话自动带上项目代码目录
- 技能、agents、命令、全局 CLAUDE.md / AGENTS.md、设置（密钥自动脱敏）、每个项目的持久记忆、Codex 记忆整体同步
- Codex 的 sqlite 用在线备份接口做一致性快照，WAL 模式下也不会损坏
- 端到端加密：restic 快照本身加密；配置和网盘授权用同一密码加密后随 git 上云，密码永不上云
- 后端向导内置：Google Drive、Cloudflare R2、OneDrive、Dropbox、WebDAV（坚果云 / Nextcloud / 群晖）、S3 兼容（OSS / COS / MinIO）、B2、SFTP、SMB、本地目录，不用开终端
- 实时进度条、网盘限速自适应、跨用户名 / 跨系统路径自动重映射
- 桌面版零依赖（内置 restic + rclone），命令行版纯 Python 标准库

## 下载

[Releases](https://github.com/majiajue/aisync/releases) 提供 macOS（Apple Silicon / Intel）、Windows、Linux 安装包，由 GitHub Actions 自动构建。


```bash
~/aisync/aisync ui        # 图形界面：http://127.0.0.1:8765
~/aisync/aisync status    # 命令行：看会同步什么
```

## 安装

| 平台 | 安装包 | 额外依赖 |
|---|---|---|
| macOS | `aisync-macos.dmg`（Apple Silicon）/ `aisync-macos-intel.dmg` | git：`xcode-select --install`。未签名，首次右键「打开」 |
| Windows | `aisync-windows.zip` → 解压 `aisync.exe` | [Git for Windows](https://git-scm.com/download/win) 或 `winget install Git.Git`；WebView2（Win10/11 自带，缺则装 [Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)） |
| Linux | `aisync-linux.tar.gz` → `chmod +x aisync` | `sudo apt install git gir1.2-webkit2-4.1` |

桌面版内置 restic 与 rclone，不用单独装。命令行版需要自装：

| 工具 | macOS | Windows（PowerShell） | Linux | 下载页 |
|---|---|---|---|---|
| restic | `brew install restic` | `winget install restic.restic` | `sudo apt install restic` | https://github.com/restic/restic/releases/latest |
| rclone | `brew install rclone` | `winget install Rclone.Rclone` | `curl https://rclone.org/install.sh \| sudo bash` | https://rclone.org/downloads/ |
| Node.js（装 claude/codex 用） | `brew install node` | `winget install OpenJS.NodeJS.LTS` | `sudo apt install nodejs npm` | https://nodejs.org/ |
| Claude Code | `npm i -g @anthropic-ai/claude-code` | 同左 | 同左 | https://docs.anthropic.com/en/docs/claude-code/setup |
| Codex | `npm i -g @openai/codex` | 同左 | 同左 | https://github.com/openai/codex |
| GitHub CLI（可选） | `brew install gh` | `winget install GitHub.cli` | `sudo apt install gh` | https://cli.github.com/ |

## 配置（应用内「安装与配置」页有同样的向导）

**1. git 远端**：在 https://github.com/new 建私有仓库 `aisync-config`。免密推送二选一：
- SSH：`ssh-keygen -t ed25519 -C aisync -f ~/.ssh/id_ed25519 -N ""`，把 `~/.ssh/id_ed25519.pub` 贴到 https://github.com/settings/ssh/new，远端填 `git@github.com:用户名/aisync-config.git`
- HTTPS：在 https://github.com/settings/tokens?type=beta 建仅限该仓库 Contents 读写的 token，远端填 `https://用户名:TOKEN@github.com/用户名/aisync-config.git`
- 或 `gh auth login && gh repo create aisync-config --private`

**2. restic 仓库**，三选一：
- Google Drive：应用内点「授权并使用 Google Drive」（等价于 `rclone config create gdrive drive scope=drive`），仓库为 `rclone:gdrive:aisync`
- Cloudflare R2（推荐，10G 免费、国内直连）：https://dash.cloudflare.com → R2 → 创建桶 `aisync` → 管理 R2 API 令牌 → 创建「对象读和写」令牌；在应用里填 Account ID / Access Key / Secret，等价于 `rclone config create r2 s3 provider=Cloudflare access_key_id=… secret_access_key=… endpoint=https://<AccountID>.r2.cloudflarestorage.com`，仓库为 `rclone:r2:aisync/aisync`
- 本地目录 / 移动硬盘 / NAS / 网盘同步文件夹：直接填路径
- 其他 70 多种后端见 https://rclone.org/overview/ ，`rclone config` 建好后仓库填 `rclone:远端名:aisync`

**3. restic 密码**：应用内生成并保存到 `~/aisync/.restic-pass`，同时存进密码管理器。丢了快照永久无法解密。

**换电脑要手动带走的三个文件**（不要上云）：`~/aisync/aisync.conf`、`~/aisync/.restic-pass`、`~/.config/rclone/rclone.conf`（Windows：`%APPDATA%\rclone\rclone.conf`）。

## 两层
- **L1 技能与配置 → git**：`~/.claude/{skills,agents,commands,CLAUDE.md,settings.json}`、每个项目的 memory、`~/.codex/{config.toml,AGENTS.md,rules,skills,memories}`。密钥自动脱敏为 `<REDACTED>`。
- **L2 会话与代码 → restic 加密快照**：勾选的 Claude/Codex 会话 jsonl、代码目录、Codex sqlite（`.backup` 一致性备份）。后端：Cloudflare R2 / Google Drive(rclone) / iCloud 目录 / 移动硬盘。

## 换机
1. 旧机器：`aisync ui` → ⚙ 设置填 git 远端 + restic 仓库 + 密码 → 勾选 → 上云。`aisync schedule` 可每 30 分钟自动同步 L1。
2. 新机器：`brew install restic rclone`，装 claude/codex，AirDrop 传 `~/aisync/{aisync,ui.py,ui.html,aisync.conf,.restic-pass}`。
3. 新机器：`aisync ui` → 从云恢复 → 拉取云端清单 → 勾选会话/代码 → 恢复到本机。用户名不同会自动 `remap`。
4. 手工补：`codex login`；`grep -rn REDACTED ~/.claude/settings.json ~/.codex/config.toml` 填回密钥；退出 Codex 后把 `~/aisync/restored/.../stage/codex/*.sqlite` 拷回 `~/.codex/`。

永不上云：`~/.codex/auth.json`、Claude keychain 凭证、restic 密码文件。

## 桌面版（macOS / Windows / Linux）
引擎 `core.py` 与界面后端 `ui.py` 都是纯 Python 标准库，三平台通用；桌面壳 `app.py` 用 pywebview 套原生窗口，PyInstaller 打包，restic 随包携带。

| 平台 | 产物 | 说明 |
|---|---|---|
| macOS | `aisync-macos.dmg` | 已安装到 `/Applications/aisync.app`。未签名，首次右键「打开」 |
| Windows | `aisync-windows.zip` → `aisync.exe` | 单文件，需要 WebView2 运行时（Win10/11 自带）和 Git for Windows |
| Linux | `aisync-linux.tar.gz` → `aisync` | 需要系统装 `gir1.2-webkit2-4.1` 和 git |

**本地打包**（在目标系统上运行）：
```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install pywebview pyinstaller
python build.py                                  # 自动下载并校验对应平台的 restic
```

**CI 一次出三平台**：把 `~/aisync` 推到 GitHub 仓库，打 tag 即触发 `.github/workflows/build.yml`，Release 里会有四个包（macOS Apple Silicon / Intel、Windows、Linux）：
```bash
git init && git add -A && git commit -m "aisync" && git remote add origin <你的仓库> && git push -u origin main
git tag v0.1.0 && git push --tags
```

命令行：mac/Linux 用 `./aisync <命令>`，Windows 用 `aisync.cmd <命令>`，都转到 `core.py`。
数据目录固定为 `~/aisync/`（Windows 为 `%USERPROFILE%\aisync`），删掉应用不影响数据。

## 协议

AGPL-3.0。你可以自由使用、修改和分发；若你修改后再分发，或以网络服务形式向他人提供，须以同样协议公开源码。详见 [LICENSE](LICENSE)。
