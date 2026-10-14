"""DevBox 环境变量同步层。

负责把各工具当前激活版本的根目录同步到用户级环境变量
（JAVA_HOME / NODE_HOME / ...），供依赖这些变量的程序使用。

设计要点：
- 只读写 HKEY_CURRENT_USER\\Environment（用户级），不触碰系统级注册表。
- 写入通过 setx 完成，写入前用 _read 比较值实现幂等，避免重复写注册表。
- 写入后广播 WM_SETTINGCHANGE，尽力通知已运行程序刷新环境，失败不影响主流程。
- 仅依赖 Python 标准库；非 Windows 平台读取返回空串，不崩溃。
"""

import subprocess

# 工具名 -> 用户级环境变量名
ENV_MAP = {
    "java": "JAVA_HOME",
    "node": "NODE_HOME",
    "python": "PYTHON_HOME",
    "php": "PHP_HOME",
    "maven": "MAVEN_HOME",
    "mysql": "MYSQL_HOME",
}

# 用户级环境变量所在的注册表子键（HKEY_CURRENT_USER\Environment）
_ENV_KEY = "Environment"


def _read(name):
    """读取用户级环境变量 name 的值；键或值不存在时返回空串。

    使用 winreg 打开 HKEY_CURRENT_USER\\Environment 并查询指定值，
    任何注册表错误（OSError）均吞掉并返回空串，保证不崩溃；
    在非 Windows 平台因导入失败同样返回空串。
    """
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _ENV_KEY, 0, winreg.KEY_READ
        ) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value or ""
    except OSError:
        return ""


def _broadcast_env_change():
    """广播 WM_SETTINGCHANGE，通知系统刷新环境变量（尽力而为，失败不影响主流程）。"""
    try:
        import ctypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            ctypes.c_wchar_p("Environment"),
            SMTO_ABORTIFHUNG,
            5000,
            ctypes.byref(result),
        )
    except Exception:
        pass


def set_env(name, value):
    """设置用户级环境变量 name 为 value。

    - 若当前值已等于目标值（用 _read 比较），直接返回，不调用 setx（幂等）；
    - 否则通过 subprocess 调用 setx <name> <value>（不加 shell，捕获输出）；
    - 写入后广播 WM_SETTINGCHANGE 让已运行程序感知变更。
    """
    if _read(name) == value:
        return
    try:
        subprocess.run(
            ["setx", name, value],
            check=False,
            capture_output=True,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        print("[DevBox] 警告：setx 执行失败 %s (%s)" % (name, exc))
        return
    _broadcast_env_change()


def read_env(name):
    """公开的读取用户级环境变量接口（供卸载残留扫描等上层使用）。"""
    return _read(name)


def _delete_env(name):
    """从 HKEY_CURRENT_USER\\Environment 删除变量；不存在或失败静默。"""
    try:
        import winreg
    except ImportError:
        return
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _ENV_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        pass


def unset_env(name):
    """删除用户级环境变量（若存在）并广播变更。

    用于卸载当前版本后清理指向已删除目录的失效 ``*_HOME``（注册表位于 C 盘）。
    """
    if _read(name) == "":
        return
    _delete_env(name)
    _broadcast_env_change()


def _is_devbox_owned(value, runtime_lower):
    """判断环境变量值是否指向 DevBox 受管 runtime_dir（即由 DevBox 写入）。"""
    if not value or not runtime_lower:
        return False
    return str(value).rstrip("\\/").lower().startswith(runtime_lower)


def sync_homes(box):
    """把各工具当前激活版本根目录同步到用户级 ``*_HOME``；无激活版本时清理失效项。

    - 有当前版本且有安装记录：``set_env(*_HOME = 版本根目录)``；
    - 无当前版本：仅当该变量此前由 DevBox 写入（值指向受管 runtime_dir）时删除，
      绝不触碰用户自建的 ``*_HOME``，避免误删。

    这样卸载某个工具的当前/唯一版本后，不会在 C 盘注册表里残留指向已删目录的变量。
    """
    runtime_lower = str(box.data.get("runtime_dir", "")).rstrip("\\/").lower()
    for tool, env_name in ENV_MAP.items():
        ver = box.current(tool)
        rec = box.find_installed(tool, ver) if ver else None
        if ver and rec:
            set_env(env_name, rec["path"])
            continue
        if ver and not rec:
            print(
                "[DevBox] 警告：%s 当前版本 %s 未找到安装记录，已跳过环境变量同步。"
                % (tool, ver)
            )
            continue
        # 无当前版本：清理此前由 DevBox 写入的失效 *_HOME
        if _is_devbox_owned(_read(env_name), runtime_lower):
            unset_env(env_name)
