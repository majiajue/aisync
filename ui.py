#!/usr/bin/env python3
"""aisync UI — 本地图形界面：勾选会话/代码/技能上云；新机器从云端清单里勾选恢复。
运行: python3 ~/aisync/ui.py  →  http://127.0.0.1:8765
只依赖 Python 标准库；push/restore 调用同目录的 aisync 脚本、git、restic。
"""
import json, os, re, subprocess, sys, threading, time, shlex, platform
import core
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HOME = Path.home()
AISYNC = Path(os.environ.get("AISYNC_HOME", HOME / "aisync"))
CLAUDE, CODEX = HOME / ".claude", HOME / ".codex"
REPO, STAGE, CACHE = AISYNC / "repo", AISYNC / "stage", AISYNC / "cache"
CONF = AISYNC / "aisync.conf"
RES = Path(os.environ.get("AISYNC_RES") or Path(__file__).parent)   # 资源目录（打包后指向 .app 内）
SELECTION = AISYNC / "selection.json"
PORT = int(os.environ.get("AISYNC_PORT", 8765))
for _d in (AISYNC, CACHE, STAGE): _d.mkdir(parents=True, exist_ok=True)
# 自带的 restic / rclone（桌面版在 res/bin，源码版在 bundle/bin）优先
os.environ["PATH"] = os.pathsep.join([str(RES / "bin"), str(RES / "bundle" / "bin"), os.environ.get("PATH", "")])

# ---------------- 配置 ----------------
read_conf, write_conf, env_for_tools = core.read_conf, core.write_conf, core.tool_env

which = core.which

# ---------------- 会话元数据 ----------------
_meta_cache_path = CACHE / "meta.json"
try: _meta = json.loads(_meta_cache_path.read_text())
except Exception: _meta = {}

def _first_lines(p, n=6000):
    with open(p, "rb") as f: return f.read(n).decode("utf-8", "ignore").split("\n")

def claude_session_meta(p: Path):
    key = f"{p}|{p.stat().st_mtime_ns}"
    if key in _meta: return _meta[key]
    title = cwd = first_ts = None
    for line in _first_lines(p, 20000):
        try: d = json.loads(line)
        except Exception: continue
        t = d.get("type")
        if t == "custom-title": title = d.get("customTitle") or title
        elif t == "ai-title" and not title: title = d.get("aiTitle")
        elif t == "user":
            cwd = cwd or d.get("cwd"); first_ts = first_ts or d.get("timestamp")
            if not title:
                c = d.get("message", {}).get("content")
                if isinstance(c, list): c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                if isinstance(c, str) and c.strip() and not c.startswith("<"): title = c.strip()[:80]
        if title and cwd and first_ts: break
    m = {"id": p.stem, "path": str(p), "title": title or "(无标题)", "cwd": cwd,
         "ts": first_ts, "mtime": p.stat().st_mtime, "size": p.stat().st_size}
    _meta[key] = m; return m

def codex_index():
    idx = {}
    f = CODEX / "session_index.jsonl"
    if f.exists():
        for line in f.read_text().splitlines():
            try: d = json.loads(line); idx[d["id"]] = d.get("thread_name")
            except Exception: pass
    return idx

def codex_session_meta(p: Path, idx):
    key = f"{p}|{p.stat().st_mtime_ns}"
    if key in _meta:
        m = _meta[key]
        if idx.get(m["id"]): m["title"] = idx[m["id"]]
        return m
    sid = cwd = ts = title = None
    for line in _first_lines(p, 300000):
        try: d = json.loads(line)
        except Exception: continue
        pl = d.get("payload", {}) or {}
        if d.get("type") == "session_meta": sid, cwd, ts = pl.get("id"), pl.get("cwd"), pl.get("timestamp")
        elif d.get("type") == "response_item" and pl.get("role") == "user" and not title:
            for c in pl.get("content") or []:
                txt = (c.get("text") or "").strip()
                if "My request for Codex:" in txt: txt = txt.split("My request for Codex:", 1)[1].strip()
                if txt.startswith("The following is the Codex agent history"): title = "(审批评估子会话)"; break
                if txt and not txt.startswith("<") and not txt.startswith("#"): title = txt.splitlines()[0][:80]; break
        if sid and title: break
    sid = sid or p.stem
    m = {"id": sid, "path": str(p), "title": idx.get(sid) or title or "(无标题)", "cwd": cwd,
         "ts": ts, "mtime": p.stat().st_mtime, "size": p.stat().st_size}
    _meta[key] = m; return m

_dirsize_path = CACHE / "dirsize.json"
try: _dirsize = json.loads(_dirsize_path.read_text())
except Exception: _dirsize = {}
_dirsize_lock = threading.Lock(); _dirsize_busy = set()

def _walk_size(p: Path, limit=60000):
    total = n = 0
    for root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", ".venv", "__pycache__", "target", "dist", "build", ".next")]
        for f in files:
            try: total += os.stat(os.path.join(root, f)).st_size
            except OSError: pass
            n += 1
            if n > limit: return total, True
    return total, False

def dir_size(p: Path):
    """异步：有缓存立刻返回，没有就后台算，先返回 None（前端显示计算中）"""
    k = str(p)
    if k in _dirsize: return _dirsize[k]["size"], _dirsize[k]["trunc"]
    with _dirsize_lock:
        if k in _dirsize_busy: return None, False
        _dirsize_busy.add(k)
    def go():
        sz, tr = _walk_size(p)
        with _dirsize_lock:
            _dirsize[k] = {"size": sz, "trunc": tr}; _dirsize_busy.discard(k)
            _dirsize_path.write_text(json.dumps(_dirsize))
    threading.Thread(target=go, daemon=True).start()
    return None, False

def inventory():
    idx = codex_index()
    claude = []
    for d in sorted((CLAUDE / "projects").iterdir()) if (CLAUDE / "projects").exists() else []:
        if not d.is_dir(): continue
        sessions = [claude_session_meta(f) for f in d.glob("*.jsonl")]
        if not sessions: continue
        cwd = next((s["cwd"] for s in sessions if s["cwd"]), None)
        claude.append({"key": d.name, "cwd": cwd, "dir": str(d), "has_memory": (d / "memory").exists(),
                       "sessions": sorted(sessions, key=lambda s: -s["mtime"])})
    codex = []
    for f in (CODEX / "sessions").rglob("*.jsonl") if (CODEX / "sessions").exists() else []:
        codex.append(codex_session_meta(f, idx))
    codex.sort(key=lambda s: -s["mtime"])
    # 代码目录：所有会话出现过的 cwd
    cwds = {}
    for pj in claude:
        for s in pj["sessions"]:
            if s["cwd"]: cwds.setdefault(s["cwd"], {"claude": 0, "codex": 0})["claude"] += 1
    for s in codex:
        if s["cwd"]: cwds.setdefault(s["cwd"], {"claude": 0, "codex": 0})["codex"] += 1
    code = []
    for c, n in sorted(cwds.items(), key=lambda kv: -(kv[1]["claude"] + kv[1]["codex"])):
        p = Path(c); ex = p.exists() and p.is_dir()
        sz, trunc = dir_size(p) if ex else (0, False)
        code.append({"path": c, "exists": ex, "git": (p / ".git").exists() if ex else False,
                     "size": sz, "size_truncated": trunc, **n})
    skills = {
        "claude_skills": sorted(x.name for x in (CLAUDE / "skills").iterdir() if x.is_dir()) if (CLAUDE / "skills").exists() else [],
        "claude_agents": sorted(x.name for x in (CLAUDE / "agents").glob("*.md")) if (CLAUDE / "agents").exists() else [],
        "claude_commands": sorted(x.name for x in (CLAUDE / "commands").rglob("*.md")) if (CLAUDE / "commands").exists() else [],
        "codex_skills": sorted(x.name for x in (CODEX / "skills").iterdir() if x.is_dir()) if (CODEX / "skills").exists() else [],
        "codex_memories": sum(1 for _ in (CODEX / "memories").rglob("*.md")) if (CODEX / "memories").exists() else 0,
        "claude_memories": sum(1 for pj in claude if pj["has_memory"]),
    }
    _meta_cache_path.write_text(json.dumps(_meta))
    return {"host": platform.node(), "user": os.environ.get("USER") or os.environ.get("USERNAME"), "home": str(HOME),
            "claude": claude, "codex": codex, "code": code, "skills": skills}

# ---------------- 云端清单 ----------------
def catalog():
    f = REPO / "catalog.json"
    if f.exists(): return json.loads(f.read_text())
    return None

def status():
    conf = read_conf()
    git_remote = None
    if (REPO / ".git").exists():
        r = subprocess.run(["git", "-C", str(REPO), "remote", "get-url", "origin"], capture_output=True, text=True)
        git_remote = r.stdout.strip() or None
    snaps = core.snapshots()
    remotes = []
    if which("rclone"):
        r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, env=env_for_tools()); remotes = [x.strip(":") for x in r.stdout.split()]
    return {"conf": {k: v for k, v in conf.items() if k != "RESTIC_PASSWORD"}, "git_remote": git_remote, "platform": sys.platform, "rclone_remotes": remotes,
            "tools": {t: which(t) for t in ("git", "restic", "rclone", "claude", "codex", "node", "gh")},
            "restic_pass": Path(conf.get("RESTIC_PASSWORD_FILE", AISYNC / ".restic-pass")).exists(),
            "snapshots": snaps[-10:], "catalog": (catalog() or {}).get("meta"), "task": TASK.snapshot()}

# ---------------- 后台任务 ----------------
class Task:
    def __init__(self): self.lines = []; self.running = False; self.name = None; self.ok = None; self.progress = None; self.lock = threading.Lock()
    def set_progress(self, d): self.progress = d
    def log(self, s):
        with self.lock: self.lines.append(s)
    def snapshot(self): return {"running": self.running, "name": self.name, "ok": self.ok, "n": len(self.lines), "progress": self.progress}
    def run(self, name, fn):
        if self.running: raise RuntimeError("已有任务在运行")
        self.lines, self.running, self.name, self.ok, self.progress = [], True, name, None, None
        def go():
            try: fn(); self.ok = True; self.log("✓ 完成")
            except Exception as e: self.ok = False; self.log(f"✗ 失败: {e}")
            finally: self.running = False
        threading.Thread(target=go, daemon=True).start()
    def sh(self, cmd, **kw):
        self.log("$ " + (cmd if isinstance(cmd, str) else " ".join(shlex.quote(c) for c in cmd)))
        p = subprocess.Popen(cmd, shell=isinstance(cmd, str), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env_for_tools(), **kw)
        for line in p.stdout: self.log(line.rstrip())
        if p.wait() != 0: raise RuntimeError(f"命令退出码 {p.returncode}")
TASK = Task()

def _hint_logger(L):
    seen = set()
    def log(m):
        L(m)
        if "rateLimitExceeded" in m and "gdrive" not in seen:
            seen.add("gdrive"); L("⚠ Google Drive API 配额耗尽（rclone 共享 client_id 的配额很小）。restic 会自动重试，速度会很慢；根治办法：到「安装与配置」的 Google Drive 卡片填自己的 Client ID / Secret 重新授权，或改用 Cloudflare R2")
        if "403" in m and "Forbidden" in m and "403" not in seen: seen.add("403"); L("⚠ 403：凭证没有写权限，检查网盘授权或密钥权限")
        if "operation not permitted" in m and "tcc" not in seen:
            seen.add("tcc"); L("⚠ macOS 拒绝访问该目录（下载/桌面/文稿/移动硬盘受隐私保护）。到 系统设置 → 隐私与安全性 → 完全磁盘访问权限 打开 aisync 的开关，然后 Cmd+Q 完全退出 aisync 再重新打开。若开关已经是开着的仍报错：用「−」删掉 aisync 再用「+」重新添加 /Applications/aisync.app（应用更新后旧授权会失效）")
        if "repository is already locked" in m and "lock" not in seen: seen.add("lock"); L("⚠ 仓库有残留锁（上次备份被中断）。下次运行会自动清理；也可现在重试一次")
    return log

def do_push(sel):
    L = _hint_logger(TASK.log)
    TASK.log("① L1 配置层 → git")
    if sel.get("config", True): core.cmd_push_config(logger=L)
    else: L("  跳过（未勾选技能与配置）")
    paths = list(sel.get("claude_sessions", [])) + list(sel.get("codex_sessions", [])) + list(sel.get("code", []))
    if sel.get("include_history"):
        paths += [str(p) for p in (CLAUDE / "history.jsonl", CODEX / "session_index.jsonl", CLAUDE / "tasks") if p.exists()]
    snap_id = None
    if paths:
        L(f"② L2 会话/代码层 → restic（{len(paths)} 个路径）")
        snap_id = core.cmd_snapshot(paths, include_sqlite=sel.get("include_sqlite", True), logger=L, progress=TASK.set_progress)
    L("③ 写入云端清单 catalog.json")
    build_catalog(set(paths), snap_id)
    core.git("add", "-A", logger=L)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO).returncode:
        core.git("commit", "-qm", f"catalog {time.strftime('%F_%T')}", logger=L)
        if read_conf().get("AISYNC_GIT_REMOTE"): core.git("push", "-q", capture=False, logger=L)
        else: L("(未配置 git 远端，仅本地提交)")

def build_catalog(chosen, snap_id=None):
    inv = inventory()
    cat = {"meta": {"host": inv["host"], "user": inv["user"], "home": inv["home"], "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "snapshot": snap_id},
           "claude": [{**pj, "sessions": [s for s in pj["sessions"] if s["path"] in chosen]} for pj in inv["claude"]],
           "codex": [s for s in inv["codex"] if s["path"] in chosen],
           "code": [c for c in inv["code"] if c["path"] in chosen], "skills": inv["skills"]}
    cat["claude"] = [pj for pj in cat["claude"] if pj["sessions"] or pj["has_memory"]]
    REPO.mkdir(parents=True, exist_ok=True)
    (REPO / "catalog.json").write_text(json.dumps(cat, ensure_ascii=False, indent=1))
    return cat

def do_restore(sel):
    L = _hint_logger(TASK.log)
    if sel.get("config", True): L("① 拉取 L1 配置层"); core.cmd_pull(logger=L)
    else: L("① 跳过技能与配置")
    cat = catalog() or {}
    old_home = (cat.get("meta") or {}).get("home")
    paths = list(sel.get("claude_sessions", [])) + list(sel.get("codex_sessions", [])) + list(sel.get("code", []))
    if sel.get("sqlite"): paths.append(str(STAGE))
    if paths:
        L(f"② 从快照 {sel.get('snapshot') or 'latest'} 恢复 {len(paths)} 个路径")
        pending = core.cmd_restore(paths, sel.get("snapshot") or "latest", old_home=old_home, logger=L, progress=TASK.set_progress)
        for p in pending: L(f"⚠ 请退出 Codex 后把 {p} 里的 sqlite 拷回 ~/.codex/")
    L("③ 体检"); core.cmd_doctor(logger=L)

def do_gdrive(name="gdrive", client_id="", client_secret=""):
    L = TASK.log
    if not which("rclone"): raise RuntimeError("未找到 rclone")
    L("① 正在打开浏览器进行 Google 授权，请在浏览器里登录并点允许…（如果没自动打开，把日志里的链接复制到浏览器）")
    extra = [f"client_id={client_id}", f"client_secret={client_secret}"] if client_id else []
    core.run(["rclone", "config", "create", name, "drive", "scope=drive", "config_is_local=true", *extra], logger=lambda m: L(m.replace(client_secret, "***") if client_secret else m))
    L("② 校验访问…"); core.run(["rclone", "lsd", f"{name}:"], check=False, logger=L)
    write_conf({"RESTIC_REPOSITORY": f"rclone:{name}:aisync"}); L(f"✓ restic 仓库已设为 rclone:{name}:aisync")

def do_r2(b):
    L = TASK.log
    for k in ("account_id", "access_key", "secret_key", "bucket"):
        if not b.get(k): raise RuntimeError(f"缺少 {k}")
    core.run(["rclone", "config", "create", "r2", "s3", "provider=Cloudflare", f"access_key_id={b['access_key']}", f"secret_access_key={b['secret_key']}",
              f"endpoint=https://{b['account_id']}.r2.cloudflarestorage.com", "acl=private", "no_check_bucket=true"], logger=lambda m: L(m.replace(b['secret_key'], '***')))
    L("校验访问…"); core.run(["rclone", "lsd", f"r2:{b['bucket']}"], check=False, logger=L)
    write_conf({"RESTIC_REPOSITORY": f"rclone:r2:{b['bucket']}/aisync"}); L(f"✓ restic 仓库已设为 rclone:r2:{b['bucket']}/aisync")

RCLONE_TYPES = {   # 类型 → (rclone 后端, 需要的字段, 是否 OAuth 拉浏览器)
    "onedrive": ("onedrive", [], True), "dropbox": ("dropbox", [], True), "box": ("box", [], True), "pcloud": ("pcloud", [], True), "gdrive2": ("drive", [], True),
    "webdav": ("webdav", ["url", "user", "pass"], False), "s3": ("s3", ["provider", "endpoint", "access_key_id", "secret_access_key"], False),
    "b2": ("b2", ["account", "key"], False), "sftp": ("sftp", ["host", "user", "pass", "port"], False), "smb": ("smb", ["host", "user", "pass"], False),
}
def do_rclone(b):
    L = TASK.log
    if not which("rclone"): raise RuntimeError("未找到 rclone")
    kind = b.get("type"); name = re.sub(r"[^A-Za-z0-9_-]", "", b.get("name") or kind)
    if kind not in RCLONE_TYPES or not name: raise RuntimeError("类型或名称不合法")
    backend, fields, oauth = RCLONE_TYPES[kind]
    params = {k: (b.get("params") or {}).get(k, "") for k in fields}
    missing = [k for k in fields if not params.get(k) and k not in ("port", "provider")]
    if missing: raise RuntimeError("缺少：" + "、".join(missing))
    secret_vals = [v for k, v in params.items() if k in ("pass", "secret_access_key", "key") and v]
    mask = lambda m: __import__("functools").reduce(lambda acc, v: acc.replace(v, "***"), secret_vals, m)
    args = []
    for k, v in params.items():
        if not v: continue
        if k == "pass":
            v = subprocess.run(["rclone", "obscure", v], capture_output=True, text=True).stdout.strip()   # rclone 要求密码字段混淆
        args.append(f"{k}={v}")
    if backend == "s3": args += ["acl=private", "no_check_bucket=true"]
    if oauth: L("① 正在打开浏览器进行授权，请登录并点允许…（没自动打开就复制日志里的链接）"); args.append("config_is_local=true")
    core.run(["rclone", "config", "create", name, backend, *args], logger=lambda m: L(mask(m)))
    path = (b.get("path") or "aisync").strip("/")
    L("② 校验访问…"); rc = core.run(["rclone", "lsd", f"{name}:{path.rsplit('/', 1)[0] if '/' in path else ''}"], check=False, logger=lambda m: L(mask(m)))
    if rc: L("⚠ 列目录失败，可能是路径还不存在或凭证有误；restic init 时会自动创建目录，可先继续")
    write_conf({"RESTIC_REPOSITORY": f"rclone:{name}:{path}"}); L(f"✓ restic 仓库已设为 rclone:{name}:{path}")

def do_git_test(remote):
    L = TASK.log
    write_conf({"AISYNC_GIT_REMOTE": remote}); core.cmd_init(logger=L)
    L(f"测试 git 远端 {remote} …"); core.run(["git", "ls-remote", "--exit-code", "-h", remote], logger=L, check=False)
    L("✓ 能访问（空仓库也会显示为成功）。若报 Permission denied，见指南里的 SSH / Token 配置")

# ---------------- HTTP ----------------
HTML = (RES / "ui.html").read_text()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", len(b)); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        try:
            if u.path == "/": b = HTML.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", len(b)); self.end_headers(); self.wfile.write(b)
            elif u.path == "/api/inventory": self._json(inventory())
            elif u.path == "/api/catalog": self._json(catalog() or {"missing": True})
            elif u.path == "/api/status": self._json(status())
            elif u.path == "/api/selection": self._json(json.loads(SELECTION.read_text()) if SELECTION.exists() else {})
            elif u.path == "/api/log":
                since = int(q.get("since", ["0"])[0]); self._json({"lines": TASK.lines[since:], "n": len(TASK.lines), **TASK.snapshot()})
            else: self._json({"error": "not found"}, 404)
        except Exception as e: self._json({"error": str(e)}, 500)
    def do_POST(self):
        u = urlparse(self.path); body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
        try:
            if u.path == "/api/selection": SELECTION.write_text(json.dumps(body, ensure_ascii=False)); self._json({"ok": True})
            elif u.path == "/api/settings":
                write_conf({"AISYNC_GIT_REMOTE": body.get("git_remote"), "RESTIC_REPOSITORY": body.get("restic_repo")})
                if body.get("restic_pass"):
                    pf = AISYNC / ".restic-pass"; pf.write_text(body["restic_pass"]); pf.chmod(0o600)
                core.cmd_init(logger=lambda *a: None)
                self._json({"ok": True})
            elif u.path == "/api/open-privacy":
                if sys.platform == "darwin": subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
                self._json({"ok": True})
            elif u.path == "/api/open":
                import webbrowser; webbrowser.open(body["url"]); self._json({"ok": True})
            elif u.path == "/api/backend/gdrive": TASK.run("gdrive", lambda: do_gdrive(body.get("name") or "gdrive", body.get("client_id", ""), body.get("client_secret", ""))); self._json({"ok": True})
            elif u.path == "/api/backend/rclone": TASK.run("rclone", lambda: do_rclone(body)); self._json({"ok": True})
            elif u.path == "/api/rclone-path": self._json({"path": __import__("shutil").which("rclone")})
            elif u.path == "/api/backend/r2": TASK.run("r2", lambda: do_r2(body)); self._json({"ok": True})
            elif u.path == "/api/backend/local":
                Path(body["path"]).mkdir(parents=True, exist_ok=True); write_conf({"RESTIC_REPOSITORY": body["path"]}); self._json({"ok": True})
            elif u.path == "/api/backend/git": TASK.run("git-test", lambda: do_git_test(body["remote"].strip())); self._json({"ok": True})
            elif u.path == "/api/setup":   # 新电脑：git 远端 + 密码 → 克隆、解密秘密、写回配置
                remote, pw = body["remote"].strip(), body["password"]
                if not remote or not pw: raise RuntimeError("git 远端和密码都要填")
                pf = AISYNC / ".restic-pass"; pf.write_text(pw); os.chmod(pf, 0o600)
                write_conf({"AISYNC_GIT_REMOTE": remote, "RESTIC_PASSWORD_FILE": str(pf)})
                def setup():
                    core.cmd_pull(logger=TASK.log)
                    if not (REPO / "secrets.enc").exists(): TASK.log("⚠ 云端没有 secrets.enc：旧电脑需先设密码并上云一次")
                    core.cmd_doctor(logger=TASK.log)
                TASK.run("setup", setup); self._json({"ok": True})
            elif u.path == "/api/restic-pass":
                pf = AISYNC / ".restic-pass"; pf.write_text(body["password"]); os.chmod(pf, 0o600); write_conf({"RESTIC_PASSWORD_FILE": str(pf)}); self._json({"ok": True})
            elif u.path == "/api/push": SELECTION.write_text(json.dumps(body, ensure_ascii=False)); TASK.run("push", lambda: do_push(body)); self._json({"ok": True})
            elif u.path == "/api/restore": TASK.run("restore", lambda: do_restore(body)); self._json({"ok": True})
            else: self._json({"error": "not found"}, 404)
        except Exception as e: self._json({"error": str(e)}, 500)

def serve(port=PORT):
    return ThreadingHTTPServer(("127.0.0.1", port), H)

if __name__ == "__main__":
    print(f"aisync UI → http://127.0.0.1:{PORT}")
    serve().serve_forever()
