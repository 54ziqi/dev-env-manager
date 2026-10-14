"""环境管理程序入口。

执行流程：
1. 加载/创建配置（``Box()``，数据统一存放在 ``D:\\环境管理\\data``，不占用 C 盘；
   自动创建目录与默认配置）；
2. 首次运行引导：确保 shim 目录已前置到系统 PATH，并扫描注册已存在的环境；
   **绝不自动下载任何内容**；
3. 启动 tkinter 图形界面（``app.gui.run_gui``）。

引导流程整体用 try/except 包裹，避免因 PATH / 注册表异常导致界面无法启动。
仅依赖 Python 标准库。
"""

import sys
import traceback

from app import systemscan
from app.config import Box
from app.gui import NOTICE_ATTR, run_gui
from app.providers import PROVIDERS, TOOL_ORDER
from app.shim import ensure_shim_in_path


# --------------------------------------------------------------------------- #
# 运行时管理员权限（UAC 自提权）
# --------------------------------------------------------------------------- #


def _is_admin():
    """判断当前进程是否已具备管理员权限；非 Windows 或探测失败一律返回 False。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - ctypes 不可用时按普通权限处理
        return False


def _ensure_admin():
    """确保以管理员权限运行；未提权时通过 UAC 重新启动自身并退出当前进程。

    MySQL 服务的安装/启停/初始化、系统 PATH 写入等操作都要求管理员权限。
    若当前进程无管理员权限，则以 ``runas`` 动词重新拉起自身触发 UAC 弹窗：

    - 提权进程成功启动后，立即退出当前非管理员进程（避免双开）；
    - 用户在 UAC 弹窗点了「否」或提权失败时，不阻塞启动，继续以普通权限运行，
      此时涉及管理员的操作会在各自结果里如实返回失败原因。

    仅在 Windows 生效；非 Windows 平台直接返回。
    """
    if sys.platform != "win32":
        return
    if _is_admin():
        return
    try:
        import ctypes

        # PyInstaller 冻结后 sys.argv[0] 即 exe 本身，参数从 [1:] 开始；
        # 开发模式下需用 python.exe 重新执行整个 argv（含脚本路径）。
        frozen = bool(getattr(sys, "frozen", False))
        exe = sys.executable
        args = sys.argv[1:] if frozen else sys.argv
        params = " ".join('"%s"' % a for a in args)
        # ShellExecuteW 返回值 > 32 表示调用成功
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, None, 1
        )
        if rc > 32:
            sys.exit(0)
    except Exception:  # noqa: BLE001 - 提权失败时退回普通权限继续运行
        pass


def _bootstrap(box):
    """首次运行引导：维护 PATH 并注册已扫描到的环境，返回需要提示的文案。

    - 调用 ``ensure_shim_in_path``；本次新加入 PATH 时返回提示文案；
    - 遍历 ``TOOL_ORDER`` 扫描已有环境，未登记过的写入 installed 记录；
    - 不下载任何内容。
    """
    notice = None

    # 1) 确保 shim 目录前置到系统 PATH（幂等）
    try:
        added = ensure_shim_in_path(box.data["shim_dir"])
    except Exception as exc:  # noqa: BLE001 - 引导流程需容忍注册表/PATH 异常
        print("[DevBox] 警告：检查 shim PATH 失败：%s" % exc)
        added = False
    if added:
        notice = (
            "已将环境管理的 shim 目录前置到系统 PATH。\n"
            "数据统一存放在 D:\\环境管理\\data，不占用 C 盘。\n"
            "请关闭所有已打开的 cmd 窗口并重新打开，使新 PATH 生效。"
        )

    # 2) 扫描并注册已有环境（不下载任何内容）
    try:
        runtime_dir = box.data["runtime_dir"]
        for tool in TOOL_ORDER:
            provider = PROVIDERS.get(tool)
            if provider is None:
                continue
            try:
                found = provider.scan_installed(runtime_dir)
            except Exception as exc:  # noqa: BLE001
                print("[DevBox] 警告：扫描 %s 失败：%s" % (tool, exc))
                continue
            for record in found or []:
                ver = record.get("ver")
                path = record.get("path")
                if not ver or not path:
                    continue
                if box.find_installed(tool, ver) is None:
                    box.add_installed(tool, ver, path, "scan")
    except Exception as exc:  # noqa: BLE001
        print("[DevBox] 警告：扫描已安装环境失败：%s" % exc)

    # 3) 扫描并注册系统已安装环境（只读扫描，静默注册，不下载、不弹窗）
    try:
        for tool in TOOL_ORDER:
            try:
                found = systemscan.scan_system(tool)
            except Exception as exc:  # noqa: BLE001
                print("[DevBox] 警告：扫描系统环境 %s 失败：%s" % (tool, exc))
                continue
            for record in (found or {}).get(tool, []) or []:
                ver = record.get("ver")
                path = record.get("path")
                if not ver or not path:
                    continue
                # 已有任何来源的同版本记录时跳过，绝不覆盖
                if box.find_installed(tool, ver) is None:
                    box.add_installed(tool, ver, path, "system")
    except Exception as exc:  # noqa: BLE001
        print("[DevBox] 警告：扫描系统环境失败：%s" % exc)

    return notice


def main():
    # 0) 运行时申请管理员权限（UAC）；提权成功则以管理员身份重启并退出当前进程
    _ensure_admin()

    # 1) 加载配置（自动创建目录与默认配置）
    box = Box()

    # 2) 首次运行引导（异常不应阻止界面启动）
    notice = None
    try:
        notice = _bootstrap(box)
    except Exception as exc:  # noqa: BLE001
        print("[DevBox] 警告：首次运行引导失败：%s" % exc)

    # 把提示转交给 GUI，在界面启动后由主线程延迟弹出
    if notice:
        setattr(box, NOTICE_ATTR, notice)

    # 3) 启动图形界面
    run_gui(box)


def _show_fatal_error(exc):
    """致命错误时以中文弹窗提示（替代 PyInstaller 英文 traceback 弹窗）。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "程序启动失败",
            "开发环境管理器启动时发生错误，请将以下信息反馈给开发者：\n\n"
            "%s\n\n"
            "详细堆栈：\n%s" % (exc, traceback.format_exc()[-800:]),
        )
        root.destroy()
    except Exception:
        # tkinter 也不可用，只能打印
        print("开发环境管理器启动失败：%s" % exc)
        print(traceback.format_exc())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        _show_fatal_error(exc)
        sys.exit(1)
