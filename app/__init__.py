"""DevBox 应用包。

包含配置层、Shim 转发、环境变量同步、版本源适配器、下载解压与 GUI 等模块。
"""


def resource_path(rel):
    """返回随程序分发的资源文件/目录的绝对路径。

    冻结（PyInstaller）环境下基于 sys._MEIPASS，源码运行时基于项目根目录。
    """
    import sys
    from pathlib import Path

    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / rel
    return Path(__file__).resolve().parent.parent / rel
