"""系统已安装开发环境扫描（纯只读）。

扫描本机系统上已经安装的 Java / Node.js / Python / PHP / Maven / MySQL，
包括未配置环境变量、未加入 PATH 的安装，供 DevBox 注册后统一管理
（切换版本、生成 shim 均复用现有机制，shim 的 ``bin_path`` 对任意根目录有效）。

候选安装根目录来源（按工具聚合后去重）：

- Windows 注册表（Python / Java，仅只读查询）；
- 常见安装目录 glob（Program Files / Program Files (x86) / C:\\ / LOCALAPPDATA 等）；
- 相关环境变量（JAVA_HOME、NVM_HOME、MAVEN_HOME 等）；
- PATH 中第一个命中的可执行文件反推根目录；
- Python 额外解析 ``py -0p`` 启动器输出。

验证方式：候选根目录必须满足 ``provider.bin_path(root) / exe_names[0]`` 存在，
随后以 ``--version`` / ``-version`` 运行该 exe 解析版本号（15 秒超时）；
解析失败、超时或 OSError 一律跳过该候选，不崩溃。

红线：本模块绝不写注册表、绝不 setx、绝不修改任何系统安装目录；
唯一的外部动作是运行 ``--version`` 读取版本号。仅依赖 Python 标准库。
"""

import glob
import os
import re
import shutil
import subprocess
from pathlib import Path

from app.providers import PROVIDERS, TOOL_ORDER

# 运行版本探测命令的默认超时（秒）。
_DEFAULT_TIMEOUT = 15

# 版本号解析正则（按顺序尝试，取第一个命中的捕获组）。
_VER_PATTERNS = {
    # java -version 输出形如：openjdk version "17.0.2" 2022-01-18（输出在 stderr）
    "java": (r'version\s+"?([^"\s]+)"?',),
    "python": (r"Python\s+(\S+)",),
    "node": (r"v?(\d+\.\d+\.\d+)",),
    "php": (r"PHP\s+(\S+)",),
    "maven": (r"Apache Maven\s+(\S+)",),
    # mysql --version 可能是 "Distrib 5.7.42" 或 "Ver 8.0.37"，优先 Distrib
    "mysql": (r"Distrib\s+(\S+)", r"Ver\s+(\S+)"),
}

# 版本探测命令行参数：java / maven 用 -version，其余用 --version。
_VERSION_FLAGS = {"java": "-version", "maven": "-version"}

# 各工具用于反推根目录/校验的环境变量。
_ENV_HOME_VARS = {
    "java": ("JAVA_HOME",),
    "node": ("NODE_HOME", "NVM_SYMLINK"),
    "python": ("PYTHON_HOME",),
    "php": ("PHP_HOME",),
    "maven": ("MAVEN_HOME", "M2_HOME"),
    "mysql": ("MYSQL_HOME",),
}

# 常见安装目录 glob 模式。占位符：{pf}=Program Files，{pf86}=Program Files (x86)，
# {appdata}=Roaming，{localappdata}=Local。
_GLOB_PATTERNS = {
    "java": (
        r"{pf}\Java\*",
        r"{pf}\Eclipse Adoptium\*",
        r"{pf}\Microsoft\jdk*",
        r"{pf}\Zulu\*",
        r"{pf}\Amazon Corretto\*",
        r"{pf86}\Java\*",
    ),
    "node": (
        r"{pf}\nodejs",  # nodejs 目录本身即根目录，不取其子目录
        r"{appdata}\nvm\*",
        r"{localappdata}\nvm\*",
    ),
    "python": (
        r"C:\Python*",
        r"{localappdata}\Programs\Python\Python*",
        r"{pf}\Python*",
    ),
    "php": (
        r"C:\php*",
        r"{pf}\PHP\*",
        r"{pf86}\PHP\*",
    ),
    "maven": (
        r"{pf}\apache-maven-*",
        r"C:\apache-maven-*",
        r"{pf86}\apache-maven-*",
    ),
    "mysql": (
        r"{pf}\MySQL\MySQL Server *",
        r"{pf86}\MySQL\MySQL Server *",
    ),
}


def _program_files():
    """返回 Program Files 目录（兼容自定义安装位置）。"""
    return os.environ.get("ProgramFiles", r"C:\Program Files")


def _program_files_x86():
    """返回 32 位 Program Files 目录。"""
    return os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")


def _appdata():
    """返回 %APPDATA%（Roaming）。"""
    return os.environ.get(
        "APPDATA", str(Path.home() / "AppData" / "Roaming")
    )


def _local_appdata():
    """返回 %LOCALAPPDATA%（Local）。"""
    return os.environ.get(
        "LOCALAPPDATA", str(Path.home() / "AppData" / "Local")
    )


def _registry_roots(tool):
    """从 Windows 注册表收集候选安装根目录（仅只读查询）。

    - Python：``SOFTWARE\\Python\\PythonCore\\<ver>\\InstallPath`` 的默认值；
    - Java：``SOFTWARE\\JavaSoft\\JDK\\<ver>\\JavaHome`` 与
      ``SOFTWARE\\JavaSoft\\Java Development Kit\\<ver>\\JavaHome``。

    HKLM / HKCU × 64 / 32 位视图都尝试；非 Windows 平台返回空列表。
    """
    try:
        import winreg
    except ImportError:
        # 非 Windows 平台没有 winreg，直接跳过注册表来源
        return []

    if tool == "python":
        base_keys = (r"SOFTWARE\Python\PythonCore",)
        value_name = "InstallPath"
    elif tool == "java":
        base_keys = (
            r"SOFTWARE\JavaSoft\JDK",
            r"SOFTWARE\JavaSoft\Java Development Kit",
        )
        value_name = "JavaHome"
    else:
        return []

    roots = []
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for hive in hives:
        for view in views:
            for base in base_keys:
                # 枚举版本子键
                versions = []
                try:
                    with winreg.OpenKey(
                        hive, base, 0, winreg.KEY_READ | view
                    ) as key:
                        index = 0
                        while True:
                            try:
                                versions.append(winreg.EnumKey(key, index))
                            except OSError:
                                break
                            index += 1
                except OSError:
                    continue

                for ver in versions:
                    sub = "%s\\%s\\%s" % (base, ver, value_name)
                    try:
                        with winreg.OpenKey(
                            hive, sub, 0, winreg.KEY_READ | view
                        ) as val_key:
                            value, _ = winreg.QueryValueEx(val_key, "")
                    except OSError:
                        continue
                    if value:
                        roots.append(str(value))
    return roots


def _glob_roots(tool):
    """按常见安装目录 glob 收集候选根目录。"""
    patterns = _GLOB_PATTERNS.get(tool, ())
    if not patterns:
        return []
    ctx = {
        "pf": _program_files(),
        "pf86": _program_files_x86(),
        "appdata": _appdata(),
        "localappdata": _local_appdata(),
    }
    roots = []
    for pattern in patterns:
        try:
            full = pattern.format(**ctx)
        except (KeyError, ValueError):
            continue
        try:
            roots.extend(glob.glob(full))
        except (OSError, re.error):
            continue
    return roots


def _env_roots(tool):
    """从相关环境变量收集候选根目录。"""
    roots = []
    for var in _ENV_HOME_VARS.get(tool, ()):
        value = os.environ.get(var, "").strip()
        if value:
            roots.append(value)

    # node 额外：NVM_HOME 的各个子目录（每个子目录是一个 Node 版本根目录）
    if tool == "node":
        nvm_home = os.environ.get("NVM_HOME", "").strip()
        if nvm_home:
            try:
                roots.extend(glob.glob(os.path.join(nvm_home, "*")))
            except (OSError, re.error):
                pass
    return roots


def _path_root(tool):
    """从 PATH 中第一个命中的主 exe 反推安装根目录；未命中返回 None。

    ``bin_sub`` 非空时根目录 = exe 父目录的父目录，否则 = exe 父目录。
    DevBox 自己的 shim 目录里是 .bat 转发脚本而非 .exe，天然不会命中。
    """
    provider = PROVIDERS.get(tool)
    if provider is None:
        return None
    try:
        exe = shutil.which(provider.exe_names[0])
    except (OSError, ValueError):
        return None
    if not exe:
        return None
    exe_path = Path(exe)
    return exe_path.parent.parent if provider.bin_sub else exe_path.parent


def _py_launcher_roots(timeout):
    """解析 ``py -0p`` 输出，收集 Python 安装根目录（仅 Python 使用）。

    输出每行形如 `` -V:3.12 *        C:\\...\\python.exe``，取 exe 路径推导根目录。
    """
    try:
        proc = subprocess.run(
            ["py", "-0p"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    roots = []
    for line in (proc.stdout or "").splitlines():
        match = re.search(r'([A-Za-z]:\\.*?python\.exe)', line, re.IGNORECASE)
        if not match:
            continue
        try:
            roots.append(Path(match.group(1)).parent)
        except (OSError, ValueError):
            continue
    return roots


def _candidate_roots(tool, timeout):
    """聚合各来源的候选根目录并去重（保留原始顺序）。"""
    roots = []
    seen = set()

    def add(path):
        """规范化并追加候选根目录；空值/非法路径/重复项跳过。"""
        if not path:
            return
        try:
            norm = str(Path(str(path).strip()))
        except (OSError, ValueError):
            return
        if not norm or norm == ".":
            return
        key = os.path.normcase(norm)
        if key in seen:
            return
        seen.add(key)
        roots.append(norm)

    for path in _registry_roots(tool):
        add(path)
    for path in _glob_roots(tool):
        add(path)
    for path in _env_roots(tool):
        add(path)
    add(_path_root(tool))
    if tool == "python":
        for path in _py_launcher_roots(timeout):
            add(path)
    return roots


def _probe_version(tool, root, timeout):
    """校验候选根目录并运行 exe 解析版本号；失败返回 None。

    校验规则：``provider.bin_path(root) / exe_names[0]`` 必须存在；
    随后运行该 exe（java/maven 用 ``-version``，其余 ``--version``）解析版本号。
    """
    provider = PROVIDERS.get(tool)
    if provider is None:
        return None

    try:
        exe = provider.bin_path(Path(root)) / provider.exe_names[0]
        if not exe.is_file():
            return None
    except (OSError, ValueError, TypeError):
        return None

    flag = _VERSION_FLAGS.get(tool, "--version")
    try:
        proc = subprocess.run(
            [str(exe), flag],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        # 解析失败/超时/OSError 一律跳过该候选，不崩溃
        return None

    # java / maven 的版本信息输出在 stderr，统一拼接后匹配
    text = "%s\n%s" % (proc.stdout or "", proc.stderr or "")
    for pattern in _VER_PATTERNS.get(tool, ()):
        match = re.search(pattern, text)
        if match:
            ver = (match.group(1) or "").strip()
            if ver:
                return ver
    return None


def discover(tool, timeout=15):
    """扫描单个工具在系统上的安装，返回 ``[{"ver","path","source":"system"}, ...]``。

    ``path`` 为安装根目录（非 bin 目录）；按 ``ver`` 去重，保留第一个命中的候选。
    工具名不存在时返回空列表。
    """
    if tool not in PROVIDERS:
        return []

    results = []
    seen = set()
    try:
        roots = _candidate_roots(tool, timeout)
    except Exception:  # noqa: BLE001 - 任何候选收集异常都不影响上层
        roots = []

    for root in roots:
        try:
            ver = _probe_version(tool, root, timeout)
        except Exception:  # noqa: BLE001
            ver = None
        if not ver or ver in seen:
            continue
        seen.add(ver)
        results.append({"ver": ver, "path": root, "source": "system"})
    return results


def scan_system(tool=None):
    """扫描系统已安装环境。``tool`` 为 None 时扫描全部 ``TOOL_ORDER``。

    返回 ``{tool: [{"ver","path","source":"system"}, ...]}``；
    ``tool`` 为不存在的工具名时返回 ``{}``。
    """
    if tool is None:
        tools = list(TOOL_ORDER)
    elif tool in PROVIDERS:
        tools = [tool]
    else:
        return {}

    result = {}
    for name in tools:
        try:
            result[name] = discover(name)
        except Exception:  # noqa: BLE001 - 单个工具失败不影响整体扫描
            result[name] = []
    return result
