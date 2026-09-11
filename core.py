#!/usr/bin/env python3
"""aisync 引擎（纯 Python，macOS / Windows / Linux 通用）
L1 config   技能/agents/命令/记忆/设置 → git
L2 sessions 会话记录/代码/sqlite     → restic 加密快照
用法: python core.py {init,status,push,pull,snapshot,restore,remap,doctor,schedule}
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

def cmd_restore(paths, snapshot="latest", old_home=None, logger=log, progress=None, targets=None):
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
    return pending

def encode_project(path: str): return re.sub(r"[\\/:]", "-", path)

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

def cmd_remap(old, new, logger=log):
    old_enc, new_enc = encode_project(old), encode_project(new)
    renamed = 0
    for d in list((CLAUDE / "projects").glob(f"{old_enc}*")) if (CLAUDE / "projects").exists() else []:
        nd = d.parent / d.name.replace(old_enc, new_enc, 1)
        if nd == d: continue
        try:
            if nd.exists(): _merge_dir(d, nd)      # 目标已存在（比如重复恢复）：合并，不再崩溃
            else: d.rename(nd)
            renamed += 1
        except Exception as e: logger(f"⚠ 项目目录 {d.name} 改名失败（{e}），跳过，不影响其它")
    n = 0
    for root in (CLAUDE / "projects", CODEX / "sessions"):
        for f in root.rglob("*.jsonl") if root.exists() else []:
            try:
                s = f.read_text(encoding="utf-8", errors="ignore")
                if old in s: f.write_text(s.replace(old, new), encoding="utf-8"); n += 1
            except Exception: pass
    logger(f"已把 {old} 重映射为 {new}（改名 {renamed} 个目录，改写 {n} 个会话文件）")

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
    elif c == "doctor": cmd_doctor()
    elif c == "schedule": cmd_schedule()
    elif c == "ui":
        import ui; print(f"http://127.0.0.1:{ui.PORT}"); ui.serve().serve_forever()
    else: print(__doc__)

if __name__ == "__main__": main(sys.argv)
