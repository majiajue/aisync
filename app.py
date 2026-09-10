#!/usr/bin/env python3
"""aisync 桌面壳：原生窗口 + 内置 HTTP 后端 + 自带 restic（macOS / Windows / Linux）"""
import os, sys, threading, socket
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
import ui, webview

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

port = free_port()
srv = ui.serve(port)
threading.Thread(target=srv.serve_forever, daemon=True).start()
webview.create_window("aisync", f"http://localhost:{port}", width=1180, height=780, min_size=(900, 600))
webview.start(debug=os.environ.get("AISYNC_DEBUG") == "1")
srv.shutdown()
