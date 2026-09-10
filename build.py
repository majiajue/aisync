#!/usr/bin/env python3
"""三平台打包脚本（在各自系统上运行；CI 见 .github/workflows/build.yml）
  python build.py            → dist/aisync-macos.dmg | dist/aisync-windows.zip | dist/aisync-linux.tar.gz
自动从 restic 官方 GitHub Release 下载对应平台二进制并校验 SHA256。
"""
import hashlib, io, os, platform, shutil, subprocess, sys, tarfile, urllib.request, zipfile, bz2
from pathlib import Path

RESTIC_VER = os.environ.get("RESTIC_VERSION", "0.19.1")
ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
IS_WIN, IS_MAC = sys.platform == "win32", sys.platform == "darwin"
arch = {"x86_64": "amd64", "AMD64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine()]
osname = "windows" if IS_WIN else "darwin" if IS_MAC else "linux"
exe = "restic.exe" if IS_WIN else "restic"

def fetch(url):
    print("↓", url); return urllib.request.urlopen(url, timeout=120).read()

def get_restic():
    bin_dir = ROOT / "bundle" / "bin"; bin_dir.mkdir(parents=True, exist_ok=True)
    dst = bin_dir / exe
    if dst.exists(): return dst
    base = f"https://github.com/restic/restic/releases/download/v{RESTIC_VER}"
    name = f"restic_{RESTIC_VER}_{osname}_{arch}" + (".zip" if IS_WIN else ".bz2")
    data = fetch(f"{base}/{name}")
    sums = fetch(f"{base}/SHA256SUMS").decode()
    want = next(l.split()[0] for l in sums.splitlines() if l.strip().endswith(name))
    got = hashlib.sha256(data).hexdigest()
    if got != want: raise SystemExit(f"restic 校验失败 {got} != {want}")
    if IS_WIN:
        with zipfile.ZipFile(io.BytesIO(data)) as z: dst.write_bytes(z.read(z.namelist()[0]))
    else:
        dst.write_bytes(bz2.decompress(data)); dst.chmod(0o755)
    print("restic", RESTIC_VER, osname, arch, "校验通过"); return dst

def get_rclone():
    """rclone 官方发布：downloads.rclone.org，按 version.txt 取最新版并校验 SHA256"""
    bin_dir = ROOT / "bundle" / "bin"; bin_dir.mkdir(parents=True, exist_ok=True)
    rexe = "rclone.exe" if IS_WIN else "rclone"; dst = bin_dir / rexe
    if dst.exists(): return dst
    ver = os.environ.get("RCLONE_VERSION") or fetch("https://downloads.rclone.org/version.txt").decode().split()[-1]   # 形如 v1.71.0
    ros = "windows" if IS_WIN else "osx" if IS_MAC else "linux"
    name = f"rclone-{ver}-{ros}-{arch}.zip"
    data = fetch(f"https://downloads.rclone.org/{ver}/{name}")
    sums = fetch(f"https://downloads.rclone.org/{ver}/SHA256SUMS").decode()
    want = next(l.split()[0] for l in sums.splitlines() if l.strip().endswith(name))
    got = hashlib.sha256(data).hexdigest()
    if got != want: raise SystemExit(f"rclone 校验失败 {got} != {want}")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        member = next(n for n in z.namelist() if n.endswith("/" + rexe)); dst.write_bytes(z.read(member))
    if not IS_WIN: dst.chmod(0o755)
    print("rclone", ver, ros, arch, "校验通过"); return dst

def build():
    get_restic(); get_rclone()
    shutil.rmtree("build", ignore_errors=True); shutil.rmtree("dist", ignore_errors=True)
    sep = ";" if IS_WIN else ":"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "aisync",
           "--add-data", f"ui.html{sep}res", "--add-data", f"ui.py{sep}res", "--add-data", f"core.py{sep}res",
           "--add-data", f"bundle/bin{sep}res/bin"]
    if IS_MAC:
        cmd += ["--osx-bundle-identifier", "com.majiajue.aisync"]
        if Path("icon.icns").exists(): cmd += ["--icon", "icon.icns"]
    elif IS_WIN:
        cmd += ["--onefile"]
        if Path("icon.ico").exists(): cmd += ["--icon", "icon.ico"]
    else:
        cmd += ["--onefile"]
    subprocess.run(cmd + ["app.py"], check=True)
    dist = ROOT / "dist"
    if IS_MAC:
        shutil.rmtree(dist / "aisync", ignore_errors=True)
        import plistlib   # 放行 http://localhost（ATS 默认拦截明文 http，窗口会空白）
        pl = dist / "aisync.app" / "Contents" / "Info.plist"; d = plistlib.loads(pl.read_bytes())
        d["NSAppTransportSecurity"] = {"NSAllowsLocalNetworking": True, "NSAllowsArbitraryLoads": True}
        d["NSLocalNetworkUsageDescription"] = "aisync 的界面通过本机回环地址与内置后端通信"
        for k in ("NSDownloadsFolderUsageDescription", "NSDesktopFolderUsageDescription", "NSDocumentsFolderUsageDescription", "NSRemovableVolumesUsageDescription", "NSNetworkVolumesUsageDescription"):
            d[k] = "aisync 需要读取该位置下你选中的代码目录以进行备份"
        d["CFBundleDisplayName"] = "aisync"; d["LSMinimumSystemVersion"] = "12.0"
        pl.write_bytes(plistlib.dumps(d))
        # 改过 Info.plist 必须重新签名，否则签名失效，macOS 的隐私授权（完全磁盘访问等）永远匹配不上
        subprocess.run(["codesign", "--force", "--deep", "--sign", os.environ.get("CODESIGN_IDENTITY", "-"), str(dist / "aisync.app")], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(dist / "aisync.app")], check=True); print("签名校验通过")
        subprocess.run(["hdiutil", "create", "-quiet", "-volname", "aisync", "-srcfolder", str(dist / "aisync.app"), "-ov", "-format", "UDZO", str(dist / "aisync-macos.dmg")], check=True)
        out = dist / "aisync-macos.dmg"
    elif IS_WIN:
        out = dist / "aisync-windows.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z: z.write(dist / "aisync.exe", "aisync.exe")
    else:
        out = dist / "aisync-linux.tar.gz"
        with tarfile.open(out, "w:gz") as t: t.add(dist / "aisync", "aisync")
    print("→", out, f"{out.stat().st_size/1048576:.1f} MB")

if __name__ == "__main__":
    build()
