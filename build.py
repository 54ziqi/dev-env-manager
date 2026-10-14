# -*- coding: utf-8 -*-
"""DevBox 完整构建流水线：打包 → Authenticode 签名 → NSIS 安装器。

用法：
    python build.py              # 全流程
    python build.py --clean      # 清理后重建
    python build.py --no-nsis    # 不生成 NSIS 安装包

环境假设：
    * Python 使用 D:\\python3.13.15\\python.exe（含 pefile/cryptography/PyInstaller）
    * NSIS：C:\\Program Files (x86)\\NSIS\\makensis.exe
    * 签名：本地 tools/authsign.py（CN=子柒 自签名证书）
    * 反逆向：DevBox.spec 已注入 runtime_hooks（反调试 + PE checksum 完整性校验）
      并 excludes 常见逆向工具链（pdb/ipdb/dis/marshal 等）

产物：
    dist/DevBox/DevBox.exe       （onedir，已 Authenticode 签名）
    dist/DevBox.zip              （onedir 压缩包）
    dist/DevBox-Setup.exe        （NSIS 安装包，已 Authenticode 签名）
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
NSI_DIR = ROOT / "installer"
MAKENSIS = Path(r"C:\Program Files (x86)\NSIS\makensis.exe")
PY_RUNNER = Path(r"D:\python3.13.15\python.exe")   # 使用带 pefile 的 Python


# ---- 命令执行 ----
def _run(cmd, cwd=None, env=None, check=True):
    print(f"\n>>> {' '.join(str(c) for c in cmd)}")
    r = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        env=env,
        # 关闭 shell，避免中文路径下 shell 参数解析出错
        shell=False,
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"命令失败 ({r.returncode}): {' '.join(map(str, cmd))}")
    return r.returncode


def _setup_env():
    """把 .build_tools/ 和 ROOT 加进 PYTHONPATH（signify / asn1crypto）。"""
    bt = ROOT / ".build_tools"
    p = os.environ.get("PYTHONPATH", "")
    parts = [str(bt), str(ROOT), p] if p else [str(bt), str(ROOT)]
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)


def _clean():
    for d in (DIST, BUILD):
        if d.exists():
            shutil.rmtree(d)
            print(f"已清理 {d.name}")
    # 清理 __pycache__ 和 build/ 下的 spec 缓存
    for pc in ROOT.rglob("__pycache__"):
        try:
            shutil.rmtree(pc)
        except Exception:
            pass


# ---- 1. PyInstaller 打包 ----
def step_pyinstaller():
    print("\n" + "=" * 60)
    print("[Step 1/3] PyInstaller 打包（含 runtime_hook 反调试）")
    print("=" * 60)
    # 使用项目 DevBox.spec（已含 runtime_hooks=runtime_hook.py）
    _run([
        str(PY_RUNNER), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "DevBox.spec",
    ], cwd=ROOT)

    exe_dir = DIST / "DevBox"
    if not (exe_dir / "DevBox.exe").exists():
        raise RuntimeError(f"打包失败：未找到 {exe_dir / 'DevBox.exe'}")
    size_mb = (exe_dir / "DevBox.exe").stat().st_size / 1024 / 1024
    print(f"  -> {exe_dir / 'DevBox.exe'} ({size_mb:.2f} MB)")


# ---- 2. Authenticode 签名 ----
def step_sign(target: Path, program_name: str = "DevBox"):
    print(f"\n>>> 签名: {target.name}")
    tools_dir = ROOT / "tools"
    r = _run([
        str(PY_RUNNER), tools_dir / "authsign.py",
        target, "sign", f"--program={program_name}",
    ], cwd=ROOT)
    if r != 0:
        raise RuntimeError(f"签名失败: {target}")


def _verify_via_subprocess(target: Path) -> dict:
    """通过子进程调用 authsign.py verify，规避同进程文件缓存不一致问题。"""
    import json as _json
    tools_dir = ROOT / "tools"
    # 强制子进程以 UTF-8 输出：authsign.py 以 ensure_ascii=False 打印中文（如"子柒"），
    # 管道场景下子进程默认使用控制台代码页(GBK)，父进程按 UTF-8 解码会抛 UnicodeDecodeError。
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [str(PY_RUNNER), str(tools_dir / "authsign.py"), str(target), "verify"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(f"签名验证失败: {target}\n{r.stdout}\n{r.stderr}")
    return _json.loads(r.stdout)


def step_sign_binary():
    print("\n" + "=" * 60)
    print("[Step 2/3] Authenticode 签名（CN=子柒）")
    print("=" * 60)
    exe = DIST / "DevBox" / "DevBox.exe"
    step_sign(exe, program_name="DevBox")
    info = _verify_via_subprocess(exe)
    print(f"  [OK] 已签名：{info['subject']}  program={info.get('program_name')}")


# ---- 4. NSIS 安装器 ----
def step_nsis():
    print("\n" + "=" * 60)
    print("[Step 4/4] NSIS 生成安装包")
    print("=" * 60)
    if not MAKENSIS.exists():
        raise RuntimeError(f"未找到 NSIS: {MAKENSIS}")

    # 确保 README.txt 存在（供安装末尾显示）
    readme = DIST / "DevBox" / "README.txt"
    if not readme.exists():
        readme.write_text(
            "开发环境管理器 - Windows 多版本开发环境管理器\n"
            "签名者：子柒\n\n"
            "使用说明：\n"
            "1. 启动 DevBox.exe 进入图形界面\n"
            "2. 添加环境（Python / Node.js / Java 等）\n"
            "3. 通过 shim 快捷命令切换到不同版本\n",
            encoding="utf-8",
        )

    # NSIS 对中文路径 + 中文字面量（子柒）非常敏感，因此把 .nsi + 资源
    # 复制到英文路径下编译，再把产物取回 dist/
    tmp_build = ROOT / "_nsi_staging"
    if tmp_build.exists():
        shutil.rmtree(tmp_build)
    tmp_build.mkdir(parents=True)
    # 复制 installer 内容
    for f in NSI_DIR.iterdir():
        if f.is_file():
            shutil.copy2(f, tmp_build / f.name)
    # 把 icon 也复制到 staging 目录下，让 NSI 里所有引用都相对 tmp_build
    src_icon = ROOT / "devbox.ico"
    if src_icon.exists():
        shutil.copy2(src_icon, tmp_build / "devbox.ico")
    # dist 已在 dist/ 下（英文）；NSI 里 OutFile 用绝对路径
    # 源 .nsi 是 UTF-8，中转时用 UTF-16 LE with BOM 保存（NSIS 强要求）
    nsi_text = (tmp_build / "DevBox.nsi").read_text(encoding="utf-8-sig")
    nsi_text = nsi_text.replace(
        '..\\dist\\DevBox-Setup.exe',
        str(DIST / "DevBox-Setup.exe"),
    ).replace(
        '..\\dist\\DevBox\\',
        str(DIST / "DevBox") + "\\",
    )
    # icon 引用保留 "devbox.ico"，因为文件已复制到 tmp_build 同目录
    # NSIS 要求 UTF-16 LE with BOM（本机 NSIS 不支持 /INPUTCHARSET=UTF8）
    (tmp_build / "DevBox.nsi").write_text(nsi_text, encoding="utf-16")

    print(f"  -> NSIS 中转目录: {tmp_build}")
    _run([
        str(MAKENSIS), "/V2",
        str(tmp_build / "DevBox.nsi"),
    ], cwd=tmp_build)

    setup = DIST / "DevBox-Setup.exe"
    if not setup.exists():
        raise RuntimeError("NSIS 未生成安装包")
    print(f"  -> {setup} ({setup.stat().st_size/1024/1024:.1f} MB)")

    # 签名安装包
    step_sign(setup, program_name="DevBox Setup")
    info = _verify_via_subprocess(setup)
    print(f"  [OK] 安装包已签名：{info['subject']}  program={info.get('program_name')}")


# ---- 5. onedir zip ----
def step_zip():
    exe_dir = DIST / "DevBox"
    zip_path = DIST / "DevBox.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in exe_dir.rglob("*"):
            zf.write(f, f.relative_to(exe_dir))
    print(f"  -> {zip_path} ({zip_path.stat().st_size/1024/1024:.1f} MB)")


# ---- 随包文档（README / LICENSE）----
_DOC_FILES = ("README.md", "README.en.md", "LICENSE")

_README_TXT = (
    "开发环境管理器 (DevBox)\r\n"
    "========================================\r\n\r\n"
    "Windows 多版本开发环境管理器：图形化下载、安装、切换\r\n"
    "Java / Node.js / Python / PHP / Maven / MySQL / Redis，\r\n"
    "并为 MySQL / Redis 提供多实例服务化管理。\r\n\r\n"
    "快速开始：\r\n"
    "  1. 启动 DevBox.exe（首次会请求管理员权限）\r\n"
    "  2. 左侧选择环境 → 「下载新版本」下载 → 「已安装版本」设为当前\r\n"
    "  3. 新开命令行窗口，java/node/mvn 等命令即指向所选版本\r\n\r\n"
    "数据目录：D:\\环境管理\\data（不占用 C 盘）\r\n\r\n"
    "更多文档（与本文件同目录）：\r\n"
    "  - README.md      中文说明\r\n"
    "  - README.en.md   English version\r\n"
    "  - LICENSE        开源许可\r\n\r\n"
    "开发者：子柒\r\n"
    "许可证：PolyForm Noncommercial License 1.0.0\r\n"
    "版本：1.0.0\r\n"
)


def step_docs():
    """把仓库根目录的 README/LICENSE 复制进 onedir 产物，随 zip 与安装包一起分发。"""
    exe_dir = DIST / "DevBox"
    if not exe_dir.is_dir():
        print("  [skip] 未找到 dist/DevBox，跳过文档附带")
        return
    for name in _DOC_FILES:
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, exe_dir / name)
            print(f"  -> 附带文档 {name}")
        else:
            print(f"  [警告] 仓库缺少 {name}，未附带")
    # 安装完成页展示的纯文本说明（UTF-8 BOM，兼容记事本正确显示中文）
    (exe_dir / "README.txt").write_text(_README_TXT, encoding="utf-8-sig")


# ---- 主流程 ----
def build(*, clean=False, no_nsis=False, nsis_only=False):
    _setup_env()
    if clean:
        _clean()

    # 清理 spec 里可能的 staged 环境
    os.environ.pop("DEVBOX_STAGED", None)

    if not nsis_only:
        step_pyinstaller()
        step_docs()
        step_sign_binary()
        step_zip()
    if not no_nsis:
        if nsis_only:
            step_docs()
        step_nsis()

    print("\n" + "=" * 60)
    print("[OK] 构建完成")
    print("=" * 60)
    print(f"  onedir: {DIST/'DevBox'/'DevBox.exe'}")
    print(f"  zip   : {DIST/'DevBox.zip'}")
    if not no_nsis:
        print(f"  setup : {DIST/'DevBox-Setup.exe'}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--no-nsis", action="store_true")
    ap.add_argument("--nsis-only", action="store_true",
                    help="仅执行 NSIS 步骤（复用 dist/ 下已有产物）")
    args = ap.parse_args()
    sys.exit(build(clean=args.clean, no_nsis=args.no_nsis, nsis_only=args.nsis_only))
