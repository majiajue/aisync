#!/usr/bin/env python3
"""aisync 引擎（纯 Python，macOS / Windows / Linux 通用）
L1 config   技能/agents/命令/记忆/设置 → git
L2 sessions 会话记录/代码/sqlite     → restic 加密快照
用法: python core.py {init,status,push,pull,snapshot,restore,remap,repair,doctor,schedule}
"""
import json, os, platform, re, shutil, sqlite3, subprocess, sys, time
from pathlib import Path

IS_WIN, IS_MAC = sys.platform == "win32", sys.platform == "darwin"
HOME = Path.home()
AISYNC = Path(os.environ.get("AISYNC_HOME") or HOME / "aisync")
CLAUDE, CODEX = HOME / ".claude", HOME / ".codex"
REPO, STAGE, CACHE = AISYNC / "repo", AISYNC / "stage", AISYNC / "cache"
CONF = AISYNC / "aisync.conf"
for _d in (AISYNC, STAGE, CACHE): _d.mkdir(parents=True, exist_ok=True)

L1_CLAUDE = ["CLAUDE.md", "settings.json", "settings.local.json", "keybindings.json",
             "skills", "agents", "commands", "output-styles",
             "plugins/installed_plugins.json", "plugins/known_marketplaces.json"]
L1_CODEX = ["config.toml", "AGENTS.md", "rules", "skills", "prompts", "memories"]
L2_SQLITE = ["thread_history_1.sqlite", "memories_1.sqlite", "state_5.sqlite", "goals_1.sqlite"]
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "target", "dist", "build", ".next"}
SECRET = re.compile(r"(key|token|secret|password|passwd)", re.I)

# Windows: 隐藏子进程（git/rclone/restic）弹出的黑色 cmd 窗口 —— 全局给 subprocess 打补丁
if IS_WIN:
    import subprocess as _sp
    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen = _sp.Popen
    class _Popen(_orig_popen):
        def __init__(self, *a, **k):
            k.setdefault("creationflags", 0); k["creationflags"] |= _CREATE_NO_WINDOW
            si = k.get("startupinfo") or _sp.STARTUPINFO()
            si.dwFlags |= _sp.STARTF_USESHOWWINDOW; si.wShowWindow = 0  # SW_HIDE
            k["startupinfo"] = si
            super().__init__(*a, **k)
    _sp.Popen = _Popen   # subprocess.run/check_output 内部都走 Popen，一处覆盖全部生效

def log(*a): print("[aisync]", *a, flush=True)
def which(x): return shutil.which(x) is not None
def hostname(): return platform.node().split(".")[0]

# ---------------- 配置 ----------------
def read_conf():
    c = {}
    if CONF.exists():
        for line in CONF.read_text(encoding="utf-8").splitlines():
            m = re.match(r'\s*(?:export\s+)?([A-Z_]+)=(.*)', line)
            if m:
                v = m.group(2).split("#")[0].strip().strip('"').strip("'")
                c[m.group(1)] = v.replace("$HOME", str(HOME)).replace("%USERPROFILE%", str(HOME))
    c.setdefault("RESTIC_PASSWORD_FILE", str(AISYNC / ".restic-pass"))
    return c

def write_conf(updates):
    c = read_conf(); c.update({k: v for k, v in updates.items() if v is not None})
    CONF.write_text("# aisync 配置\n" + "".join(f'{k}="{v}"\n' for k, v in c.items()), encoding="utf-8")

def tool_env():
    e = dict(os.environ); e.update({k: v for k, v in read_conf().items() if k.startswith("RESTIC")}); e.setdefault("RESTIC_PROGRESS_FPS", "1"); return e   # 非终端下 restic 默认 60s 才报一次进度

def run(cmd, check=True, capture=False, cwd=None, logger=log, progress=None):
    logger("$ " + " ".join(str(c) for c in cmd if c != "--json"))
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=tool_env(), encoding="utf-8", errors="replace")
        if check and r.returncode: raise RuntimeError(f"{cmd[0]} 失败: {r.stderr.strip()[:400]}")
        return r.stdout
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd, env=tool_env(), encoding="utf-8", errors="replace")
    for line in p.stdout:
        line = line.rstrip()
        if progress and line.startswith("{"):
            try: d = json.loads(line)
            except Exception: logger(line); continue
            t = d.get("message_type")
            if t == "status":
                progress({"percent": round(100 * float(d.get("percent_done", 0)), 1), "done": d.get("bytes_done") or d.get("bytes_restored") or 0,
                          "total": d.get("total_bytes") or 0, "eta": d.get("seconds_remaining"), "files": d.get("files_done") or d.get("files_restored"), "total_files": d.get("total_files")})
            elif t == "summary":
                progress({"percent": 100, "done": d.get("total_bytes_processed") or d.get("total_bytes") or 0, "total": d.get("total_bytes_processed") or d.get("total_bytes") or 0, "eta": 0})
                if "snapshot_id" in d: logger(f"快照 {d['snapshot_id'][:8]}：新增 {d.get('files_new',0)} 文件，变更 {d.get('files_changed',0)}，上传 {d.get('data_added',0)/1048576:.1f} MB，用时 {d.get('total_duration',0):.0f}s")
                else: logger(f"恢复 {d.get('files_restored',0)}/{d.get('total_files',0)} 文件，{d.get('bytes_restored',0)/1048576:.1f} MB")
            elif t == "error": logger("⚠ " + str(d.get("error", {}).get("message") or d.get("item") or line))
            elif t == "exit_error": logger(("⚠ " if d.get("code") == 3 else "✗ ") + str(d.get("message", line)))
            elif t in ("verbose_status",): pass
            else: logger(line)
        else: logger(line)
    if p.wait() and check: raise RuntimeError(f"{cmd[0]} 退出码 {p.returncode}")
    return p.returncode

# ---------------- 脱敏 ----------------
def scrub_settings(src, dst):
    d = json.loads(Path(src).read_text(encoding="utf-8"))
    for k in list(d.get("env", {})):
        if SECRET.search(k): d["env"][k] = f"<REDACTED:{k}>"
    Path(dst).write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")

def scrub_toml(src, dst):
    s = Path(src).read_text(encoding="utf-8")
    s = re.sub(r'("--key",\s*")[^"]+(")', r'\1<REDACTED>\2', s)
    s = re.sub(r'^(\s*[A-Za-z_]*(?:key|token|secret|password)[A-Za-z_]*\s*=\s*")[^"]+(")', r'\1<REDACTED>\2', s, flags=re.I | re.M)
    Path(dst).write_text(s, encoding="utf-8")

def copytree(src: Path, dst: Path):
    """镜像复制（删除目标中多余文件），跳过 .git / node_modules 等"""
    if dst.exists(): shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=lambda d, names: [n for n in names if n in SKIP_DIRS or n.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm"))], symlinks=True)

# ---------------- L1 ----------------
def stage_l1(logger=log):
    (REPO / "claude" / "projects").mkdir(parents=True, exist_ok=True); (REPO / "codex").mkdir(exist_ok=True)
    for p in L1_CLAUDE:
        s = CLAUDE / p
        if not s.exists(): continue
        d = REPO / "claude" / p; d.parent.mkdir(parents=True, exist_ok=True)
        if p == "settings.json": scrub_settings(s, d)
        elif s.is_dir(): copytree(s, d)
        else: shutil.copy2(s, d)
    for m in (CLAUDE / "projects").glob("*/memory") if (CLAUDE / "projects").exists() else []:
        copytree(m, REPO / "claude" / "projects" / m.parent.name / "memory")
    for p in L1_CODEX:
        s = CODEX / p
        if not s.exists(): continue
        d = REPO / "codex" / p
        if p == "config.toml": scrub_toml(s, d)
        elif s.is_dir(): copytree(s, d)
        else: shutil.copy2(s, d)
    def ver(c):
        try: return subprocess.run([c, "--version"], capture_output=True, text=True, timeout=10, shell=IS_WIN).stdout.strip()[:60]
        except Exception: return None
    (REPO / "manifest.json").write_text(json.dumps({
        "host": platform.node(), "user": os.environ.get("USER") or os.environ.get("USERNAME"), "home": str(HOME), "os": sys.platform,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "claude_version": ver("claude"), "codex_version": ver("codex"),
        "claude_projects": sorted(x.name for x in (CLAUDE / "projects").iterdir()) if (CLAUDE / "projects").exists() else []},
        indent=1, ensure_ascii=False), encoding="utf-8")
    export_desktop_index(REPO / "desktop-index", logger=logger)
    export_codex_index(REPO / "codex-index", logger=logger)
    if not (REPO / ".gitattributes").exists(): (REPO / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    if not (REPO / ".gitignore").exists(): (REPO / ".gitignore").write_text("auth.json\n*.sqlite*\n", encoding="utf-8")

def git(*args, cwd=REPO, check=True, capture=True, logger=log): return run(["git", *args], cwd=str(cwd), check=check, capture=capture, logger=logger)

def cmd_init(git_remote=None, restic_repo=None, logger=log):
    if not which("git"): raise RuntimeError("缺少 git" + ("：安装 Git for Windows" if IS_WIN else "：xcode-select --install 或 apt install git"))
    if not (REPO / ".git").exists():
        REPO.mkdir(parents=True, exist_ok=True); git("init", "-q", logger=logger); git("config", "advice.addEmbeddedRepo", "false", logger=logger)
    write_conf({"AISYNC_GIT_REMOTE": git_remote, "RESTIC_REPOSITORY": restic_repo})
    logger(f"已初始化 {AISYNC}")

def cmd_push_config(logger=log):
    if not (REPO / ".git").exists(): cmd_init(logger=logger)
    stage_l1(logger); pack_secrets(logger)
    git("add", "-A", logger=logger)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO).returncode == 0:
        logger("L1 无变化"); return
    git("commit", "-qm", f"sync from {hostname()} {time.strftime('%F_%T')}", logger=logger)
    remote = read_conf().get("AISYNC_GIT_REMOTE")
    if remote:
        if subprocess.run(["git", "remote", "get-url", "origin"], cwd=REPO, capture_output=True).returncode: git("remote", "add", "origin", remote, logger=logger)
        git("push", "-q", "-u", "origin", "HEAD", capture=False, logger=logger)
        logger("L1 已推送")
    else: logger("L1 已本地提交（未配置 git 远端）")

def cmd_pull(logger=log):
    remote = read_conf().get("AISYNC_GIT_REMOTE")
    if not (REPO / ".git").exists():
        if not remote: raise RuntimeError("先在设置里填 git 远端")
        run(["git", "clone", "-q", remote, str(REPO)], logger=logger)
    elif remote: git("pull", "-q", "--rebase", capture=False, check=False, logger=logger)
    pw = restic_password()
    if pw and (REPO / "secrets.enc").exists():
        try: unpack_secrets(pw, logger); write_conf({"AISYNC_GIT_REMOTE": remote})
        except ValueError as e: logger(f"⚠ secrets.enc 解密失败：{e}")
    bak = AISYNC / f"backup-{int(time.time())}"
    logger(f"写回前把本机现有文件备份到 {bak}")
    for side, root in (("claude", CLAUDE), ("codex", CODEX)):
        src = REPO / side
        if not src.exists(): continue
        for f in src.rglob("*"):
            if f.is_dir() or ".git" in f.parts: continue
            rel = f.relative_to(src); dst = root / rel
            if dst.exists():
                b = bak / side / rel; b.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(dst, b)
            dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(f, dst)
    logger("L1 已写回。密钥占位符需手工填回：搜索 <REDACTED>")

# ---------------- L2 ----------------
def restic_ok():
    if not which("restic"): raise RuntimeError("未安装 restic（桌面版自带；命令行请 brew/winget/apt install restic）")
    c = read_conf()
    if not c.get("RESTIC_REPOSITORY"): raise RuntimeError("未配置 restic 仓库")
    if not Path(c["RESTIC_PASSWORD_FILE"]).exists(): raise RuntimeError("未设置 restic 密码")

def sqlite_backup(src: Path, dst: Path):
    """用 sqlite 在线备份 API，WAL 模式下也一致，不需要 sqlite3 命令行"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True); d = sqlite3.connect(str(dst))
    with d: s.backup(d)
    s.close(); d.close()

def cmd_snapshot(paths, include_sqlite=True, logger=log, progress=None):
    restic_ok()
    if subprocess.run(["restic", "cat", "config"], capture_output=True, env=tool_env()).returncode: run(["restic", "init"], logger=logger)
    paths = [p for p in paths if Path(p).exists()]
    if include_sqlite:
        for s in L2_SQLITE:
            if (CODEX / s).exists(): logger(f"sqlite 备份 {s}"); sqlite_backup(CODEX / s, STAGE / "codex" / s)
        paths.append(str(STAGE))
    listf = CACHE / "backup-paths.txt"; listf.write_text("\n".join(paths), encoding="utf-8")
    repo = read_conf().get("RESTIC_REPOSITORY", ""); tune = []
    run(["restic", "unlock"], check=False, logger=lambda m: None)   # 清理上次被中断留下的失效锁（只删无进程持有的锁）
    if repo.startswith("rclone:"):   # 网盘 API 有频率限制（尤其 Google Drive 共享 client_id）：降并发、大分块、限速
        os.environ["RESTIC_PACK_SIZE"] = "64"
        tune = ["-o", "rclone.connections=2", "-o", "rclone.timeout=15m", "-o", "rclone.args=serve restic --stdio --transfers 2 --checkers 2 --tpslimit 4 --tpslimit-burst 2 --retries 15 --low-level-retries 30 --timeout 15m --contimeout 2m --expect-continue-timeout 30s --drive-pacer-min-sleep 500ms --drive-chunk-size 32M --drive-acknowledge-abuse"]
        logger("网盘后端：并发降到 2 路，分块 64M，限速 4 请求/秒")
    rc = run(["restic", "backup", "--json", *tune, "--files-from-verbatim", str(listf), "--tag", "aisync", "--tag", hostname(),
         *sum((["--exclude", e] for e in ("node_modules", ".venv", "__pycache__", "*.tmp", ".DS_Store", "Thumbs.db")), []), "--exclude-caches"], check=False, logger=logger, progress=progress)
    if rc == 3: logger("⚠ 快照已创建，但上面列出的文件/目录没读到。macOS 上通常是权限问题：系统设置 → 隐私与安全性 → 完全磁盘访问权限，把 aisync 加进去后重新上云")
    elif rc: raise RuntimeError(f"restic 退出码 {rc}")
    run(["restic", "forget", *tune, "--tag", "aisync", "--keep-daily", "14", "--keep-weekly", "8", "--keep-monthly", "12", "--prune", "-q"], check=False, logger=logger)
    out = run(["restic", "snapshots", "--json", "--latest", "1", "--tag", "aisync"], capture=True, logger=logger)
    try: return json.loads(out)[-1]["short_id"]
    except Exception: return None

def snapshots():
    if not which("restic") or not read_conf().get("RESTIC_REPOSITORY"): return []
    r = subprocess.run(["restic", "snapshots", "--json", "--tag", "aisync"], capture_output=True, text=True, env=tool_env())
    try: return [{"id": s["short_id"], "time": s["time"], "host": s["hostname"], "paths": s["paths"]} for s in json.loads(r.stdout)] if r.returncode == 0 else []
    except Exception: return []

def _restored_location(target: Path, original: str):
    """restic 把绝对路径还原到 target 下。原路径可能是异系统的（macOS 存 /Users/..，Windows 存 C:\\Users\\..），
    所以自己按分隔符切段，不用 pathlib（它会用本机规则误解析）。"""
    segs = [s for s in re.split(r"[\\/]", original) if s and s not in (".", "..")]
    if segs and re.fullmatch(r"[A-Za-z]:", segs[0]): segs[0] = segs[0][0]   # "C:" -> "C"
    cands = [target.joinpath(*segs)]                      # 含盘符：target/C/Users/..
    if segs and re.fullmatch(r"[A-Za-z]", segs[0]): cands.append(target.joinpath(*segs[1:]))  # 不含盘符：target/Users/..
    return next((c for c in cands if c.exists()), None)

def _remap_home(path_str: str, old_home: str):
    """把异系统的旧 home 前缀换成本机 home，分隔符统一成本机的。只处理 home 下的路径（会话/技能等都在 ~/.claude|.codex）。"""
    norm = path_str.replace("\\", "/"); oh = (old_home or "").replace("\\", "/").rstrip("/")
    if oh and norm.startswith(oh + "/"):
        rel = norm[len(oh) + 1:]
        return str(HOME / Path(*rel.split("/")))
    return path_str

def cmd_restore(paths, snapshot="latest", old_home=None, logger=log, progress=None, targets=None, desktop_index=True):
    """targets: {原路径: 新路径}，用于把代码目录恢复到自定义位置；会话里的 cwd 也会同步改写"""
    targets = {k: v for k, v in (targets or {}).items() if v and v != k}
    """先还原到临时目录，再搬到原位（跨平台一致），返回未能自动就位的路径"""
    restic_ok()
    target = AISYNC / "restored" / time.strftime("%Y%m%d-%H%M%S"); target.mkdir(parents=True)
    run(["restic", "unlock"], check=False, logger=lambda m: None)
    tune = ["-o", "rclone.connections=2", "-o", "rclone.timeout=15m", "-o", "rclone.args=serve restic --stdio --transfers 2 --tpslimit 4 --retries 15 --low-level-retries 30 --timeout 15m --contimeout 2m --drive-pacer-min-sleep 500ms"] if read_conf().get("RESTIC_REPOSITORY", "").startswith("rclone:") else []
    ea = [0]
    def _rlog(m):
        if "set EA failed" in m or "extended attribute" in m or "restore metadata" in m: ea[0] += 1
        logger(m)
    rc = run(["restic", "restore", "--json", "--exclude-xattr", "*", *tune, snapshot, "--target", str(target), *sum((["--include", p] for p in paths), [])], logger=_rlog, progress=progress, check=False)
    if rc and ea[0] and ea[0] >= 1: logger(f"⚠ {ea[0]} 处扩展属性未写入（跨系统 macOS 元数据，Windows 不支持），文件内容已完整恢复，忽略即可")
    elif rc: raise RuntimeError(f"restic restore 退出码 {rc}")
    pending = []
    for p in paths:
        src = _restored_location(target, p)
        if not src: logger(f"⚠ 快照里没有 {p}"); continue
        dst = Path(targets[p]) if p in targets else Path(_remap_home(p, old_home))
        if str(src).startswith(str(STAGE)) or p.startswith(str(STAGE)):  # sqlite 留在 restored，需退出 Codex 后拷回
            pending.append(str(src)); continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir(): shutil.copytree(src, dst, dirs_exist_ok=True)
        else: shutil.copy2(src, dst)
        logger(f"✓ {dst}")
    try:
        if old_home and old_home != str(HOME): cmd_remap(old_home, str(HOME), logger=logger)
        for old_p, new_p in targets.items():   # 代码目录换了位置：把会话记录里的 cwd 也改过来，claude --resume / codex resume 才能对上
            op = _remap_home(old_p, old_home) if old_home else old_p
            cmd_remap(old_p, new_p, logger=logger)
            if op != old_p: cmd_remap(op, new_p, logger=logger)
    except Exception as e:
        logger(f"⚠ 路径重映射时出错（{e}），但文件已恢复到位；会话若在 Claude 里对不上目录，可重开一次 aisync 恢复")
    if desktop_index:
        pm = dict(targets)
        if old_home and old_home != str(HOME): pm.setdefault(old_home, str(HOME))
        try: import_desktop_index(REPO / "desktop-index", path_map=pm, logger=logger)
        except Exception as e: logger(f"⚠ 写回 Claude 桌面版索引失败（{e}），不影响已恢复的会话")
        try:
            import_codex_index(REPO / "codex-index", path_map=pm, logger=logger)
            import_codex_global_state(REPO / "codex-index", path_map=pm, logger=logger)
        except Exception as e: logger(f"⚠ 写回 Codex 索引失败（{e}），不影响已恢复的会话")
    return pending

def encode_project(path: str):
    """Claude 的项目目录名编码：非字母数字的字符（含 / \\ : . _ 和所有中文）一律变 '-'。
    已用本机 11 个项目全部验证通过。注意这是有损的：中文路径无法从目录名反推。"""
    return re.sub(r"[^A-Za-z0-9]", "-", path)

def _merge_dir(src: Path, dst: Path):
    """把 src 目录合并进 dst（dst 已存在时逐文件搬，冲突覆盖），最后删掉空的 src。"""
    dst.mkdir(parents=True, exist_ok=True)
    for item in list(src.iterdir()):
        target = dst / item.name
        if item.is_dir(): _merge_dir(item, target)
        else:
            if target.exists(): target.unlink()
            shutil.move(str(item), str(target))
    try: src.rmdir()
    except OSError: pass

def session_cwd(f: Path):
    """取会话文件的归属 cwd：第一条 user 消息的 cwd（与 inventory 的判定一致）。"""
    try:
        with open(f, "rb") as fh: head = fh.read(200000).decode("utf-8", "ignore")
    except OSError: return None
    for line in head.split("\n"):
        if '"cwd"' not in line: continue
        try: d = json.loads(line)
        except Exception: continue
        if d.get("type") == "user" and d.get("cwd"): return d["cwd"]
    return None

def cmd_remap(old, new, logger=log):
    """把旧路径 old 下的会话改到新路径 new。
    关键：项目目录名必须由【新的 cwd】重新编码得出，不能在旧目录名上做字符串替换——
    旧目录名里中文早已被编码成 '-'，无法还原（交付包 -> ---）。"""
    old_n, new_n = old.replace("\\", "/").rstrip("/"), new.replace("\\", "/").rstrip("/")
    n_files = n_dirs = 0
    root = CLAUDE / "projects"
    for d in sorted(root.iterdir()) if root.exists() else []:
        if not d.is_dir(): continue
        for f in list(d.glob("*.jsonl")):
            cwd = session_cwd(f)
            if not cwd: continue
            c_n = cwd.replace("\\", "/").rstrip("/")
            if not (c_n == old_n or c_n.startswith(old_n + "/")): continue
            new_cwd = new + c_n[len(old_n):].replace("/", "\\" if IS_WIN else "/")
            # 1) 改写会话内的路径：先把本会话的完整 cwd 换成新 cwd（分隔符正确），
            #    再把其余仍以旧根开头的路径也换过去。两种写法（原样 / 正斜杠）都覆盖。
            # 会话是 JSON Lines。逐行解析后在【数据结构层】改路径再序列化回去，
            # 绝不对原始文本做正则/字符串替换——那样极易产生非法转义把整行弄坏。
            def _map(v):
                if not isinstance(v, str): return v
                n = v.replace("\\", "/").rstrip("/")
                for base, dest in ((c_n, new_cwd), (old_n, new)):
                    if not base: continue
                    if n == base: return dest
                    if n.startswith(base + "/"):
                        tail = n[len(base):]
                        return dest + (tail.replace("/", "\\") if IS_WIN else tail)
                return v
            def _walk(o):
                if isinstance(o, str): return _map(o)
                if isinstance(o, list): return [_walk(x) for x in o]
                if isinstance(o, dict): return {k: _walk(x) for k, x in o.items()}
                return o
            try:
                lines = f.read_text(encoding="utf-8", errors="ignore").split("\n")
                out, changed = [], False
                for ln in lines:
                    t = ln.strip()
                    if not t: out.append(ln); continue
                    try: obj = json.loads(t)
                    except Exception: out.append(ln); continue   # 解析不了就原样保留，绝不破坏
                    new_obj = _walk(obj)
                    if new_obj != obj:
                        out.append(json.dumps(new_obj, ensure_ascii=False)); changed = True
                    else: out.append(ln)
                if changed:
                    f.write_text("\n".join(out), encoding="utf-8"); n_files += 1
            except Exception as e: logger(f"⚠ 改写 {f.name} 失败：{e}"); continue
            # 2) 按新 cwd 重新编码目录名，把会话挪过去
            want = root / encode_project(new_cwd)
            if want == f.parent: continue
            try:
                want.mkdir(parents=True, exist_ok=True)
                tgt = want / f.name
                if tgt.exists(): tgt.unlink()
                shutil.move(str(f), str(tgt)); n_dirs += 1
            except Exception as e: logger(f"⚠ 移动 {f.name} 到 {want.name} 失败：{e}")
        try:
            if d.exists() and not any(d.iterdir()): d.rmdir()
        except OSError: pass
    logger(f"已把 {old} 重映射为 {new}（改写 {n_files} 个会话，归位 {n_dirs} 个到新项目目录）")

def cmd_repair(logger=log):
    """就地修复：按每个会话自己的 cwd 重新计算项目目录名并归位。
    用于恢复后目录名编码不对（例如含中文的路径）导致 Claude 不显示会话。不联网。"""
    root = CLAUDE / "projects"
    if not root.exists(): logger("没有 ~/.claude/projects"); return 0
    moved = 0
    for d in sorted([x for x in root.iterdir() if x.is_dir()]):
        for f in list(d.glob("*.jsonl")):
            cwd = session_cwd(f)
            if not cwd: continue
            want = root / encode_project(cwd)
            if want == f.parent: continue
            try:
                want.mkdir(parents=True, exist_ok=True)
                tgt = want / f.name
                if tgt.exists(): tgt.unlink()
                shutil.move(str(f), str(tgt)); moved += 1
                logger(f"  {f.parent.name} -> {want.name}  ({f.name})")
            except Exception as e: logger(f"⚠ {f.name} 移动失败：{e}")
        try:
            if d.exists() and not any(d.iterdir()): d.rmdir()
        except OSError: pass
    logger(f"修复完成：归位 {moved} 个会话到正确的项目目录")
    return moved

# ---------------- 桌面版会话索引（侧栏靠它显示，不是 ~/.claude/projects） ----------------
# 结构：<AppSupport>/Claude/claude-code-sessions/<accountUuid>/<profileUuid>/local_<uuid>.json
# 两层 uuid 是本机账号/设备标识，换机后不同，所以恢复时要探测目标机自己的目录，不能照搬。
DESKTOP_DIRS = {
    "darwin": Path.home() / "Library/Application Support/Claude",
    "win32": Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Claude",
}
# 只保留可移植字段；机器相关的（Chrome 标签组、MCP 配置、工具快照等）一律不带
INDEX_KEEP = {"sessionId", "cliSessionId", "cwd", "originCwd", "title", "titleSource",
              "createdAt", "lastActivityAt", "lastFocusedAt", "model", "effort",
              "isArchived", "permissionMode", "completedTurns"}

def desktop_root():
    d = DESKTOP_DIRS.get(sys.platform) or (Path.home() / ".config/Claude")
    return d / "claude-code-sessions"

def desktop_index_dir(create=False):
    """返回本机的 <account>/<profile> 索引目录；换机后 uuid 不同，靠探测而非硬编码。"""
    root = desktop_root()
    if not root.exists(): return None
    cands = sorted(root.glob("*/*"), key=lambda p: -p.stat().st_mtime)
    cands = [c for c in cands if c.is_dir()]
    return cands[0] if cands else None

def export_desktop_index(dst: Path, logger=log):
    """把桌面版索引里的会话条目导出到 repo（只留可移植字段）。"""
    d = desktop_index_dir()
    if not d: logger("  未发现 Claude 桌面版索引，跳过"); return 0
    dst.mkdir(parents=True, exist_ok=True)
    for f in dst.glob("*.json"): f.unlink()
    n = 0
    for f in d.glob("local_*.json"):
        try: obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception: continue
        slim = {k: v for k, v in obj.items() if k in INDEX_KEEP}
        if not slim.get("cliSessionId"): continue
        (dst / f.name).write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
    logger(f"  桌面版会话索引：导出 {n} 条")
    return n

def import_desktop_index(src: Path, path_map=None, logger=log):
    """把索引写回本机桌面版。只写 cliSessionId 能在本机找到 jsonl 的条目，
    并按 path_map 改写 cwd；已存在的同名条目不覆盖（保护本机现有会话列表）。"""
    if not src.exists(): logger("  云端没有桌面版索引，跳过"); return 0
    d = desktop_index_dir()
    if not d:
        logger("  本机未安装 Claude 桌面版（或未登录），跳过索引恢复"); return 0
    have = {p.stem for p in (CLAUDE / "projects").rglob("*.jsonl")} if (CLAUDE / "projects").exists() else set()
    n = skipped = 0
    for f in sorted(src.glob("local_*.json")):
        try: obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception: continue
        cli = obj.get("cliSessionId")
        if cli not in have: skipped += 1; continue      # 没恢复对应会话就不要造孤儿条目
        for k in ("cwd", "originCwd"):
            if obj.get(k) and path_map:
                for old, new in path_map.items():
                    o = old.replace("\\", "/").rstrip("/"); c = obj[k].replace("\\", "/").rstrip("/")
                    if c == o: obj[k] = new; break
                    if c.startswith(o + "/"):
                        tail = c[len(o):]
                        obj[k] = new + (tail.replace("/", "\\") if IS_WIN else tail); break
        tgt = d / f.name
        if tgt.exists(): skipped += 1; continue          # 不覆盖本机已有条目
        tgt.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
    logger(f"  桌面版会话索引：写入 {n} 条，跳过 {skipped} 条（本机已有或无对应会话）")
    if n: logger("  ⚠ 重启 Claude 桌面版后侧栏才会刷新")
    return n

# ---------------- Codex 桌面端会话索引（侧栏靠 state_5.sqlite 的 threads 表） ----------------
# CODEX_HOME 跨平台都是 ~/.codex，不像 Claude 桌面版要探测 App Support。
# threads 主键是 id，且 74/74 与 rollout 文件名 uuid 一致，所以能逐行改写路径并按 id 合并。
CODEX_STATE_DB = "state_5.sqlite"
# 全局状态里只带可移植的键；electron-persisted-atom-state 894KB（含 accountId、MCP 目录、
# prompt 历史）和窗口位置、设备 token 一律不带。
GLOBAL_KEEP_PLAIN = {"thread-titles", "project-order", "pinned-thread-ids", "selected-project",
                     "sidebar-project-thread-orders", "thread-project-assignments",
                     "electron-thread-read-state-v1", "electron-initial-follow-up-queue-mode"}
GLOBAL_KEEP_PATHS = {"local-projects", "electron-saved-workspace-roots",
                     "electron-workspace-root-labels", "active-workspace-roots"}

def _remap_path_str(v, path_map):
    if not isinstance(v, str) or not path_map: return v
    n = v.replace("\\", "/").rstrip("/")
    for old, new in path_map.items():
        o = old.replace("\\", "/").rstrip("/")
        if not o: continue
        if n == o: return new
        if n.startswith(o + "/"):
            tail = n[len(o):]
            return new + (tail.replace("/", "\\") if IS_WIN else tail)
    return v

def export_codex_index(dst: Path, logger=log):
    """导出 Codex 侧栏会话列表（threads 表）+ 全局状态的可移植部分。"""
    db = CODEX / CODEX_STATE_DB
    if not db.exists(): logger("  未发现 Codex state 数据库，跳过"); return 0
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True); c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute("select * from threads")]
        tools = [dict(r) for r in c.execute("select * from thread_dynamic_tools")]
        c.close()
        (dst / "threads.json").write_text(json.dumps({"threads": rows, "thread_dynamic_tools": tools},
                                                      ensure_ascii=False, indent=1), encoding="utf-8")
        n = len(rows)
    except Exception as e:
        logger(f"  ⚠ 读取 Codex threads 失败：{e}"); return 0
    gs = CODEX / ".codex-global-state.json"
    if gs.exists():
        try:
            d = json.loads(gs.read_text(encoding="utf-8"))
            slim = {k: v for k, v in d.items() if k in GLOBAL_KEEP_PLAIN | GLOBAL_KEEP_PATHS}
            (dst / "global-state.json").write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e: logger(f"  ⚠ 读取 Codex 全局状态失败：{e}")
    logger(f"  Codex 会话索引：导出 {n} 条线程")
    return n

def import_codex_index(src: Path, path_map=None, logger=log):
    """写回 Codex 侧栏列表。只写本机确实有 rollout 文件的线程；
    按主键 id 用 INSERT OR IGNORE 合并，绝不覆盖目标机已有线程。"""
    f = src / "threads.json"
    if not f.exists(): logger("  云端没有 Codex 索引，跳过"); return 0
    db = CODEX / CODEX_STATE_DB
    if not db.exists(): logger("  本机未安装 Codex（无 state 数据库），跳过"); return 0
    try: data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e: logger(f"  ⚠ 读取云端 Codex 索引失败：{e}"); return 0
    # 本机实际存在的 rollout：uuid -> 路径。只写有对应文件的线程，避免造出点开就报错的空条目。
    local = {}
    sess = CODEX / "sessions"
    if sess.exists():
        for p in sess.rglob("rollout-*.jsonl"):
            m = re.search(r"-([0-9a-f-]{36})\.jsonl$", p.name)
            if m: local[m.group(1)] = str(p)
    rows = [r for r in data.get("threads", []) if r.get("id") in local]
    if not rows: logger("  没有可写入的线程（本机缺少对应的 rollout 文件）"); return 0
    n = 0
    try:
        c = sqlite3.connect(str(db))
        cols = [d[1] for d in c.execute("PRAGMA table_info(threads)")]
        for r in rows:
            r = dict(r)
            r["rollout_path"] = local.get(r["id"], r.get("rollout_path"))
            r["cwd"] = _remap_path_str(r.get("cwd"), path_map)
            vals = [r.get(k) for k in cols]
            q = f"INSERT OR IGNORE INTO threads ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
            cur = c.execute(q, vals); n += cur.rowcount
        ids = {r["id"] for r in rows}
        tcols = [d[1] for d in c.execute("PRAGMA table_info(thread_dynamic_tools)")]
        for t in data.get("thread_dynamic_tools", []):
            if t.get("thread_id") not in ids: continue
            q = f"INSERT OR IGNORE INTO thread_dynamic_tools ({','.join(tcols)}) VALUES ({','.join('?' * len(tcols))})"
            c.execute(q, [t.get(k) for k in tcols])
        c.commit(); c.close()
    except Exception as e:
        logger(f"  ⚠ 写入 Codex 索引失败：{e}"); return 0
    logger(f"  Codex 会话索引：新增 {n} 条线程（已存在的不动）")
    if n: logger("  ⚠ 重启 Codex 后侧栏才会刷新")
    return n

def import_codex_global_state(src: Path, path_map=None, logger=log):
    """合并 Codex 全局状态的可移植键；路径类的按 path_map 改写；本机已有键不覆盖。"""
    f = src / "global-state.json"
    gs = CODEX / ".codex-global-state.json"
    if not f.exists() or not gs.exists(): return 0
    try:
        incoming = json.loads(f.read_text(encoding="utf-8"))
        cur = json.loads(gs.read_text(encoding="utf-8"))
    except Exception as e: logger(f"  ⚠ 读取 Codex 全局状态失败：{e}"); return 0
    def fix(o):
        if isinstance(o, str): return _remap_path_str(o, path_map)
        if isinstance(o, list): return [fix(x) for x in o]
        if isinstance(o, dict): return {k: fix(v) for k, v in o.items()}
        return o
    n = 0
    for k, v in incoming.items():
        v = fix(v) if k in GLOBAL_KEEP_PATHS else v
        if k not in cur: cur[k] = v; n += 1
        elif isinstance(cur[k], dict) and isinstance(v, dict):
            for kk, vv in v.items():
                if kk not in cur[k]: cur[k][kk] = vv; n += 1
        elif isinstance(cur[k], list) and isinstance(v, list):
            for item in v:
                if item not in cur[k]: cur[k].append(item); n += 1
    shutil.copy2(gs, gs.with_suffix(".json.aisync-bak"))
    gs.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    logger(f"  Codex 全局状态：合并 {n} 项（原文件已备份为 .aisync-bak）")
    return n

# ---------------- 秘密打包：用 restic 密码加密后随 git 走 ----------------
import hashlib, hmac, struct, secrets as _secrets

def _chacha20_block(key, counter, nonce):
    c = [0x61707865, 0x3320646e, 0x79622d32, 0x6b206574, *struct.unpack("<8L", key), counter, *struct.unpack("<3L", nonce)]
    x = c[:]
    def qr(a, b, cc, d):
        x[a] = (x[a] + x[b]) & 0xffffffff; x[d] ^= x[a]; x[d] = ((x[d] << 16) | (x[d] >> 16)) & 0xffffffff
        x[cc] = (x[cc] + x[d]) & 0xffffffff; x[b] ^= x[cc]; x[b] = ((x[b] << 12) | (x[b] >> 20)) & 0xffffffff
        x[a] = (x[a] + x[b]) & 0xffffffff; x[d] ^= x[a]; x[d] = ((x[d] << 8) | (x[d] >> 24)) & 0xffffffff
        x[cc] = (x[cc] + x[d]) & 0xffffffff; x[b] ^= x[cc]; x[b] = ((x[b] << 7) | (x[b] >> 25)) & 0xffffffff
    for _ in range(10):
        qr(0, 4, 8, 12); qr(1, 5, 9, 13); qr(2, 6, 10, 14); qr(3, 7, 11, 15)
        qr(0, 5, 10, 15); qr(1, 6, 11, 12); qr(2, 7, 8, 13); qr(3, 4, 9, 14)
    return struct.pack("<16L", *[(x[i] + c[i]) & 0xffffffff for i in range(16)])

def _chacha20(key, nonce, data):
    out = bytearray(); i = 0
    while i < len(data):
        block = _chacha20_block(key, 1 + i // 64, nonce); chunk = data[i:i + 64]
        out += bytes(a ^ b for a, b in zip(chunk, block)); i += 64
    return bytes(out)

def _keys(password: str, salt: bytes):
    k = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=128 * 1024 * 1024, dklen=64); return k[:32], k[32:]

def encrypt(password: str, data: bytes) -> bytes:
    """ChaCha20 + HMAC-SHA256（encrypt-then-MAC），密钥由 scrypt 派生。格式 AIS1|salt16|nonce12|ct|tag32"""
    salt, nonce = _secrets.token_bytes(16), _secrets.token_bytes(12); ek, mk = _keys(password, salt)
    ct = _chacha20(ek, nonce, data); tag = hmac.new(mk, b"AIS1" + salt + nonce + ct, hashlib.sha256).digest()
    return b"AIS1" + salt + nonce + ct + tag

def decrypt(password: str, blob: bytes) -> bytes:
    if blob[:4] != b"AIS1": raise ValueError("不是 aisync 加密文件")
    salt, nonce, ct, tag = blob[4:20], blob[20:32], blob[32:-32], blob[-32:]; ek, mk = _keys(password, salt)
    if not hmac.compare_digest(hmac.new(mk, b"AIS1" + salt + nonce + ct, hashlib.sha256).digest(), tag): raise ValueError("密码错误或文件损坏")
    return _chacha20(ek, nonce, ct)

def rclone_conf_path():
    if os.environ.get("RCLONE_CONFIG"): return Path(os.environ["RCLONE_CONFIG"])
    return Path(os.environ.get("APPDATA", HOME / "AppData/Roaming")) / "rclone" / "rclone.conf" if IS_WIN else HOME / ".config" / "rclone" / "rclone.conf"

def restic_password():
    pf = Path(read_conf()["RESTIC_PASSWORD_FILE"]); return pf.read_text(encoding="utf-8").strip() if pf.exists() else None

def pack_secrets(logger=log):
    """把 aisync.conf / rclone.conf 用 restic 密码加密写入 repo/secrets.enc（restic 密码本身不写入）"""
    pw = restic_password()
    if not pw: logger("未设置 restic 密码，跳过秘密打包"); return
    conf = {k: v for k, v in read_conf().items() if k not in ("RESTIC_PASSWORD_FILE", "AISYNC_GIT_REMOTE")}
    rc = rclone_conf_path()
    bundle = {"os": sys.platform, "home": str(HOME), "conf": conf, "rclone_conf": rc.read_text(encoding="utf-8") if rc.exists() else None, "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (REPO / "secrets.enc").write_bytes(encrypt(pw, json.dumps(bundle, ensure_ascii=False).encode()))
    logger("秘密已加密打包（aisync.conf + rclone.conf）")

def unpack_secrets(password: str, logger=log):
    """新电脑：用密码解开 repo/secrets.enc，按本系统写回配置"""
    f = REPO / "secrets.enc"
    if not f.exists(): raise RuntimeError("云端没有 secrets.enc，旧电脑需先设置 restic 密码并上云一次")
    b = json.loads(decrypt(password, f.read_bytes()).decode())
    pf = AISYNC / ".restic-pass"; pf.write_text(password, encoding="utf-8")
    try: pf.chmod(0o600)
    except OSError: pass
    conf = b["conf"]; conf["RESTIC_PASSWORD_FILE"] = str(pf)
    if IS_WIN != (b["os"] == "win32") and conf.get("RESTIC_REPOSITORY") and not conf["RESTIC_REPOSITORY"].startswith(("rclone:", "s3:", "sftp:", "rest:", "b2:", "azure:", "gs:")):
        logger(f"⚠ 旧电脑的 restic 仓库是本地路径 {conf['RESTIC_REPOSITORY']}，跨系统请在设置里改成本机可访问的路径")
    write_conf(conf)
    if b.get("rclone_conf"):
        rc = rclone_conf_path(); rc.parent.mkdir(parents=True, exist_ok=True)
        if rc.exists(): shutil.copy2(rc, rc.with_suffix(".conf.bak"))
        rc.write_text(b["rclone_conf"], encoding="utf-8"); logger(f"rclone 配置已写到 {rc}")
    logger(f"✓ 配置已从云端恢复（来自 {b['os']} {b['home']} @ {b['time']}）")

# ---------------- 其他 ----------------
def cmd_doctor(logger=log):
    out = []
    mf = REPO / "manifest.json"
    if mf.exists():
        m = json.loads(mf.read_text(encoding="utf-8"))
        out.append(f"来源机器: {m['host']} {m['user']} {m['home']} @ {m['updated']}")
        if m["home"] != str(HOME): out.append(f"⚠ home 不同：{m['home']} → {HOME}，恢复时会自动重映射")
    for t, hint in (("claude", "npm i -g @anthropic-ai/claude-code"), ("codex", "npm i -g @openai/codex"), ("git", "安装 git")):
        out.append(f"{t}: {'✓' if which(t) else '⚠ 未安装：' + hint}")
    for f in (CLAUDE / "settings.json", CODEX / "config.toml"):
        if f.exists() and "REDACTED" in f.read_text(encoding="utf-8", errors="ignore"): out.append(f"⚠ {f.name} 仍有 <REDACTED> 占位符未填回")
    if not (CODEX / "auth.json").exists(): out.append("⚠ Codex 未登录：codex login")
    for l in out: logger("  " + l)
    return out

def cmd_schedule(minutes=30, logger=log):
    exe = os.environ.get("AISYNC_BIN") or f'"{sys.executable}" "{Path(__file__).resolve()}"'
    if IS_MAC:
        pl = HOME / "Library/LaunchAgents/com.aisync.push.plist"
        args = "".join(f"<string>{a}</string>" for a in [*(exe.replace('"', '').split(" ") if "python" in exe else [exe]), "push", "--config-only"])
        pl.write_text(f'<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>Label</key><string>com.aisync.push</string><key>ProgramArguments</key><array>{args}</array><key>StartInterval</key><integer>{minutes*60}</integer><key>StandardOutPath</key><string>{AISYNC}/push.log</string><key>StandardErrorPath</key><string>{AISYNC}/push.log</string></dict></plist>')
        subprocess.run(["launchctl", "unload", str(pl)], capture_output=True); subprocess.run(["launchctl", "load", str(pl)], check=True)
    elif IS_WIN:
        subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(minutes), "/TN", "aisync-push", "/TR", f'{exe} push --config-only'], check=True)
    else:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        cron = "\n".join(l for l in cron.splitlines() if "aisync" not in l) + f"\n*/{minutes} * * * * {exe} push --config-only >> {AISYNC}/push.log 2>&1\n"
        subprocess.run(["crontab", "-"], input=cron, text=True, check=True)
    logger(f"已安装定时任务：每 {minutes} 分钟同步 L1")

def cmd_status():
    print("L1（git）:")
    for p in L1_CLAUDE: (CLAUDE / p).exists() and print(f"  claude/{p}")
    for p in L1_CODEX: (CODEX / p).exists() and print(f"  codex/{p}")
    print("L2（restic）: 由 UI 勾选，或 push --all 全部会话")
    c = read_conf(); print(f"git 远端: {c.get('AISYNC_GIT_REMOTE') or '未设'}   restic: {c.get('RESTIC_REPOSITORY') or '未设'}")
    for s in snapshots()[-5:]: print(f"  快照 {s['id']} {s['time'][:16]} {s['host']}")

def main(argv):
    a = argv[1:] if len(argv) > 1 else ["help"]
    c = a[0]
    if c == "init": cmd_init(*(a[1:3]))
    elif c == "status": cmd_status()
    elif c == "push":
        cmd_push_config()
        if "--config-only" not in a:
            paths = [str(CLAUDE / "projects"), str(CLAUDE / "history.jsonl"), str(CODEX / "sessions"), str(CODEX / "session_index.jsonl")]
            print("快照:", cmd_snapshot(paths))
    elif c == "snapshot": print("快照:", cmd_snapshot([str(CLAUDE / "projects"), str(CODEX / "sessions")]))
    elif c == "pull": cmd_pull(); cmd_doctor()
    elif c == "restore": print("待手工处理:", cmd_restore([str(CLAUDE / "projects"), str(CODEX / "sessions")], a[1] if len(a) > 1 else "latest")); cmd_doctor()
    elif c == "remap": cmd_remap(a[1], a[2] if len(a) > 2 else str(HOME))
    elif c == "repair": cmd_repair()
    elif c == "doctor": cmd_doctor()
    elif c == "schedule": cmd_schedule()
    elif c == "ui":
        import ui; print(f"http://127.0.0.1:{ui.PORT}"); ui.serve().serve_forever()
    else: print(__doc__)

if __name__ == "__main__": main(sys.argv)
