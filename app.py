#!/usr/bin/env python3
"""aisync 桌面壳：原生窗口 + 内置 HTTP 后端 + 自带 restic（macOS / Windows / Linux）"""
import os, sys, threading, socket
os.environ.setdefault("PYTHONUTF8", "1"); os.environ.setdefault("PYTHONIOENCODING", "utf-8")   # Windows 默认 GBK，子进程（git/restic 输出）也统一 UTF-8
from pathlib import Path

RES = Path(sys._MEIPASS) / "res" if hasattr(sys, "_MEIPASS") else Path(__file__).parent   # 打包后资源在 res/
os.environ["AISYNC_RES"] = str(RES)
os.environ.setdefault("AISYNC_HOME", str(Path.home() / "aisync"))
# 自带的 restic 放到 PATH 最前面；GUI 启动的进程拿不到 shell PATH，把常见位置补上
extra = [str(RES / "bin")]
if sys.platform == "darwin": extra += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
elif sys.platform.startswith("linux"): extra += ["/usr/local/bin", "/usr/bin", "/bin", str(Path.home() / ".local/bin")]
else: extra += [str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd"), str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd")]
os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])
if sys.platform != "win32":
    for f in (RES / "bin").glob("*"):
        try: os.chmod(f, 0o755)
        except OSError: pass

sys.path.insert(0, str(RES))

# 命令行子命令（repair / doctor / push ...）：GUI 包默认没有控制台，先挂到父终端再交给引擎处理。
# 不加这段的话 aisync.exe repair 会被忽略、直接弹出窗口，看起来「跑完什么都没发生」。
if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
    if sys.platform == "win32":
        try:
            import ctypes
            if ctypes.windll.kernel32.AttachConsole(-1):   # ATTACH_PARENT_PROCESS
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        except Exception: pass
    import core
    core.main(sys.argv)
    raise SystemExit(0)

import ui, webview

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

port = free_port()
srv = ui.serve(port)
threading.Thread(target=srv.serve_forever, daemon=True).start()
webview.create_window("aisync", f"http://localhost:{port}", width=1180, height=780, min_size=(900, 600))
webview.start(debug=os.environ.get("AISYNC_DEBUG") == "1")
srv.shutdown()
