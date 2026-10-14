"""DevBox MySQL 服务端实例管理模块（UI 无关，便于单独测试）。

MySQL 与其它开发环境不同，需要区分两件事：

1. **客户端版本切换**：由既有 Shim 机制完成，``mysql.exe`` 始终来自当前选中
   版本的 ``bin`` 目录，用户无需关心具体路径。这部分本模块不负责，也不修改。
2. **服务端实例管理**：本模块负责。``mysqld`` 必须注册为 Windows 服务才能常驻
   运行。为了让多个 MySQL 版本共存，本模块按版本创建相互独立的实例：

   - 每个版本使用独立的 **配置目录**（``<runtime_dir>/mysql/<ver>``）存放 my.ini；
   - 每个版本使用独立的 **端口**（3306 起按排序依次递增）；
   - 每个版本使用独立的 **数据目录**（``<版本根目录>/data``）；
   - 每个版本注册为 **唯一服务名**（``MySQL80_<版本号下划线化>``）。

   由此可以初始化实例、安装服务、启动、停止、卸载，并从错误日志中提取
   ``--initialize`` 生成的临时密码。

设计约定：
- 仅依赖 Python 标准库（``re`` / ``subprocess`` / ``pathlib``）。
- 所有外部命令调用都带 ``capture_output`` / ``text`` / 容错编码（Windows 中文控制台
  通常为 GBK，故用 ``encoding='gbk', errors='replace'``）与 ``timeout``，并用
  ``try/except`` 包裹，异常统一转成 ``{'ok': False, 'message': ...}``，绝不让上层崩溃。
- 所有写入 ini 的路径统一使用正斜杠，避免反斜杠被转义。
- 不自动提权：涉及管理员权限的命令失败时，如实把 mysqld/sc/net 的输出返回给用户。
- 端口分配是稳定的：同样的已安装版本列表必然得到同样的端口，便于重复调用。
"""

import re
import subprocess
import time
from pathlib import Path

from app.providers import PROVIDERS

# 命令默认超时（秒）
_TIMEOUT_SHORT = 30     # 快速查询类命令（sc query）
_TIMEOUT_NORMAL = 120   # 服务安装/启停类命令
_TIMEOUT_INIT = 300     # mysqld --initialize 可能较慢

# MySQL 8 初始化后写入错误日志的临时密码格式：
#   A temporary password is generated for root@localhost: xxxxxx
_TEMP_PWD_RE = re.compile(
    r"temporary password is generated[^:]*:\s*(\S+)", re.IGNORECASE
)

# sc query 的 STATE 数值 -> 中文状态文本
_STATE_TEXT = {
    1: "已停止",
    2: "正在启动",
    3: "正在停止",
    4: "运行中",
    5: "正在继续",
    6: "正在暂停",
    7: "已暂停",
}


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _version_root(box, ver):
    """返回该版本的安装根目录 Path；未登记时抛出 RuntimeError。"""
    record = box.find_installed("mysql", ver)
    if not record or not record.get("path"):
        raise RuntimeError("未找到 MySQL %s 的安装记录，请先安装该版本。" % ver)
    return Path(record["path"])


def _bin_dir(box, ver):
    """返回该版本的 bin 目录（mysqld.exe 所在目录）。"""
    return PROVIDERS["mysql"].bin_path(_version_root(box, ver))


def _run(cmd, cwd=None, timeout=_TIMEOUT_NORMAL, log_cb=None):
    """执行外部命令，返回 ``(ok, output)``；任何异常都转成 ``(False, 文本)``。

    ``output`` 为 stdout 与 stderr 合并后的文本（已 strip）。``log_cb`` 为可选回调，
    用于把命令本身与输出回传给调用方（通常投递到 UI 主线程）。
    """
    if log_cb:
        try:
            log_cb("> " + " ".join(str(c) for c in cmd))
        except Exception:  # noqa: BLE001 - 回调异常不得影响主流程
            pass

    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        msg = "命令不存在或无法执行：%s" % cmd[0]
        if log_cb:
            try:
                log_cb(msg)
            except Exception:  # noqa: BLE001
                pass
        return False, msg
    except subprocess.TimeoutExpired:
        msg = "命令执行超时（%s 秒）。" % timeout
        if log_cb:
            try:
                log_cb(msg)
            except Exception:  # noqa: BLE001
                pass
        return False, msg
    except OSError as exc:
        msg = "命令执行失败：%s" % exc
        if log_cb:
            try:
                log_cb(msg)
            except Exception:  # noqa: BLE001
                pass
        return False, msg

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if log_cb and output:
        try:
            log_cb(output)
        except Exception:  # noqa: BLE001
            pass
    return proc.returncode == 0, output


def _read_temp_password(log_path):
    """从错误日志文件读取并提取临时密码；文件不存在或未匹配返回 None。"""
    try:
        path = Path(log_path)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _TEMP_PWD_RE.search(text)
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# 命名 / 端口 / 目录
# --------------------------------------------------------------------------- #


def service_name(ver):
    """返回该版本的唯一 Windows 服务名，形如 ``MySQL80_8_0_40``（ver='8.0.40'）。"""
    return "MySQL80_" + str(ver).replace(".", "_")


def instance_port(box, ver):
    """为该版本分配独立端口。

    把 ``box.installed_list('mysql')`` 里的版本号排序，返回 ``3306 + 下标``
    （第一个为 3306，第二个为 3307，以此类推）；若版本不在列表中，用列表长度作为下标。
    同样的已安装列表必然得到同样的结果，端口分配稳定可重复。
    """
    vers = sorted(str(r.get("ver", "")) for r in box.installed_list("mysql"))
    ver = str(ver)
    if ver in vers:
        index = vers.index(ver)
    else:
        index = len(vers)
    return 3306 + index


def instance_dir(box, ver):
    """返回该版本的实例配置目录：``<runtime_dir>/mysql/<ver>``。"""
    return Path(box.data["runtime_dir"]) / "mysql" / str(ver)


def write_my_ini(box, ver, init_file=None):
    """在实例目录生成（覆盖写入）my.ini，返回该 ini 的 Path。

    版本根目录取 ``box.find_installed('mysql', ver)['path']``，ini 中的路径统一
    使用正斜杠，端口取 ``instance_port``。``init_file`` 不为空时额外写入
    ``init-file=...``（服务启动早期以超级用户执行其中的 SQL，用于重置密码）。

    关键：必须用 GBK 写入。mysqld 在中文 Windows 上按系统 ANSI 代码页(936/GBK)
    读取选项文件；若用 UTF-8，含中文的 basedir/datadir（如 D:\环境管理）会被
    误解码成乱码，导致初始化报“Can't create directory / errmsg.sys 找不到 / 数据目录不可用”。
    """
    root = _version_root(box, ver)
    root_fwd = str(root).replace("\\", "/")
    port = instance_port(box, ver)

    ini = instance_dir(box, ver) / "my.ini"
    ini.parent.mkdir(parents=True, exist_ok=True)

    extra = ""
    if init_file:
        extra = "init-file=%s\n" % str(init_file).replace("\\", "/")
    content = (
        "[mysqld]\n"
        "basedir=%s\n"
        "datadir=%s/data\n"
        "port=%d\n"
        "log-error=%s/data/error.log\n"
        "%s"
        "\n"
        "[client]\n"
        "port=%d\n"
    ) % (root_fwd, root_fwd, port, root_fwd, extra, port)
    ini.write_text(content, encoding="gbk")
    return ini


def is_datadir_empty(box, ver):
    """判断 datadir（``<版本根目录>/data``）是否为空；不存在视为空。"""
    data = _version_root(box, ver) / "data"
    if not data.is_dir():
        return True
    try:
        return not any(data.iterdir())
    except OSError:
        return False


def read_temp_password(box, ver):
    """读取该版本错误日志中的临时密码；未找到返回 None。"""
    try:
        root = _version_root(box, ver)
    except RuntimeError:
        return None
    return _read_temp_password(root / "data" / "error.log")


# --------------------------------------------------------------------------- #
# 实例生命周期
# --------------------------------------------------------------------------- #


def initialize(box, ver, log_cb=None):
    """初始化实例。

    - 若数据目录非空，抛出 ``RuntimeError``（'数据目录非空，跳过初始化'）；
    - 写入 my.ini；
    - 运行 ``mysqld --defaults-file=<my.ini> --initialize``（cwd 为 bin 目录，
      合并捕获 stdout/stderr，超时 300 秒）；
    - 从 ``<版本根目录>/data/error.log`` 提取临时密码。

    返回 ``{'ok': bool, 'password': str|None, 'message': str}``。
    ``log_cb`` 为可选回调，用于把过程文本回传（UI 用）。
    """
    # 数据目录非空属于调用方逻辑错误，按约定抛出友好的 RuntimeError
    if not is_datadir_empty(box, ver):
        raise RuntimeError("数据目录非空，跳过初始化")

    try:
        ini = write_my_ini(box, ver)
        bin_dir = _bin_dir(box, ver)
        mysqld = bin_dir / "mysqld.exe"
        cmd = [str(mysqld), "--defaults-file=%s" % str(ini), "--initialize"]
        ok, output = _run(cmd, cwd=bin_dir, timeout=_TIMEOUT_INIT, log_cb=log_cb)

        password = _read_temp_password(_version_root(box, ver) / "data" / "error.log")
        if ok:
            if password:
                message = "初始化完成。root@localhost 临时密码：%s" % password
            else:
                message = "初始化完成，但未在错误日志中找到临时密码。"
        else:
            message = output or "初始化失败，请检查数据目录权限。"
        return {"ok": bool(ok), "password": password, "message": message}
    except Exception as exc:  # noqa: BLE001 - 统一兜底为结果字典
        return {"ok": False, "password": None, "message": "初始化失败：%s" % exc}


def install_service(box, ver, log_cb=None):
    """安装 Windows 服务：``mysqld --install <服务名> --defaults-file=<my.ini>``。

    需要管理员权限；失败时把 mysqld 的输出作为 message 返回，不抛未捕获异常。
    返回 ``{'ok', 'message'}``。
    """
    try:
        ini = write_my_ini(box, ver)
        bin_dir = _bin_dir(box, ver)
        mysqld = bin_dir / "mysqld.exe"
        name = service_name(ver)
        cmd = [str(mysqld), "--install", name, "--defaults-file=%s" % str(ini)]
        ok, output = _run(cmd, cwd=bin_dir, timeout=_TIMEOUT_NORMAL, log_cb=log_cb)
        if ok:
            return {"ok": True, "message": "服务 %s 安装成功。" % name}
        # 服务可能已存在（mysqld --install 对重名服务会失败）：明确提示而非误导权限。
        if _service_exists(name):
            return {"ok": True, "message": "服务 %s 已存在，无需重复安装。" % name}
        return {"ok": False, "message": output or "服务安装失败，请确认以管理员身份运行。"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "服务安装失败：%s" % exc}


def start_service(box, ver, log_cb=None):
    """启动服务：优先 ``net start <服务名>``，失败回退 ``sc start``。"""
    name = service_name(ver)
    try:
        ok, output = _run(["net", "start", name], timeout=_TIMEOUT_NORMAL, log_cb=log_cb)
        if ok:
            return {"ok": True, "message": "服务 %s 已启动。" % name}
        # 已在运行时 net 会返回非零，视为成功
        if "已经启动" in output or "already" in output.lower():
            return {"ok": True, "message": "服务 %s 已经在运行。" % name}

        ok2, output2 = _run(["sc", "start", name], timeout=_TIMEOUT_NORMAL, log_cb=log_cb)
        if ok2:
            return {"ok": True, "message": "服务 %s 已启动（sc start）。" % name}
        return {
            "ok": False,
            "message": output2 or output or "启动服务失败，请确认以管理员身份运行。",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "启动服务失败：%s" % exc}


def stop_service(box, ver, log_cb=None):
    """停止服务：``net stop <服务名>``。"""
    name = service_name(ver)
    try:
        ok, output = _run(["net", "stop", name], timeout=_TIMEOUT_NORMAL, log_cb=log_cb)
        if ok:
            return {"ok": True, "message": "服务 %s 已停止。" % name}
        if "未启动" in output or "not started" in output.lower():
            return {"ok": True, "message": "服务 %s 当前未运行。" % name}
        return {
            "ok": False,
            "message": output or "停止服务失败，请确认以管理员身份运行。",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "停止服务失败：%s" % exc}


def uninstall_service(box, ver, log_cb=None):
    """卸载服务：先尝试停止，再 ``mysqld --remove <服务名>``，失败回退 ``sc delete``。"""
    name = service_name(ver)
    try:
        # 先尝试停止，忽略其结果（服务可能本来就没运行或未安装）
        _run(["net", "stop", name], timeout=_TIMEOUT_NORMAL, log_cb=log_cb)

        bin_dir = _bin_dir(box, ver)
        mysqld = bin_dir / "mysqld.exe"
        ok, output = _run(
            [str(mysqld), "--remove", name],
            cwd=bin_dir,
            timeout=_TIMEOUT_NORMAL,
            log_cb=log_cb,
        )
        if ok:
            return {"ok": True, "message": "服务 %s 已卸载。" % name}

        ok2, output2 = _run(["sc", "delete", name], timeout=_TIMEOUT_NORMAL, log_cb=log_cb)
        if ok2:
            return {"ok": True, "message": "服务 %s 已删除（sc delete）。" % name}
        return {
            "ok": False,
            "message": output2 or output or "卸载服务失败，请确认以管理员身份运行。",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "卸载服务失败：%s" % exc}


def service_status(box, ver):
    """返回服务状态的中文文本；未安装或查询失败返回 '未安装' 之类的文本。"""
    name = service_name(ver)
    try:
        ok, output = _run(["sc", "query", name], timeout=_TIMEOUT_SHORT)
    except Exception:  # noqa: BLE001 - _run 已兜底，这里再加一层防御
        return "未安装"
    if not ok:
        return "未安装"

    # sc 输出的字段名可能为英文 STATE 或本地化的「状态」，此处兼容两者
    match = re.search(r"(?:STATE|状态)\s*:\s*(\d+)", output)
    if match:
        return _STATE_TEXT.get(int(match.group(1)), "未知")
    return "未知"


def service_installed(box, ver):
    """判断该版本的 Windows 服务是否已安装（供卸载残留检查使用）。"""
    return _service_exists(service_name(ver))


def running_processes(box, ver):
    """返回属于该版本的运行中 mysqld 进程列表（供卸载残留检查使用）。"""
    return _matching_procs(box, ver)


# --------------------------------------------------------------------------- #
# 进程管理 / SQL 执行
# --------------------------------------------------------------------------- #


def _service_exists(name):
    """判断 Windows 服务是否已安装（sc query 返回 1060 表示不存在）。"""
    ok, output = _run(["sc", "query", name], timeout=_TIMEOUT_SHORT)
    if ok:
        return True
    low = (output or "").lower()
    return not ("1060" in low or "does not exist" in low or "不存在" in output)


def _terminate(proc):
    """终止后台进程：先 terminate，超时再 kill，失败忽略。"""
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _list_mysqld_procs():
    """通过 PowerShell 枚举当前运行的 mysqld.exe，返回 ``[(pid, exe_path, cmdline), ...]``。

    同时取可执行路径与命令行：服务方式启动的 mysqld 其 ``ExecutablePath`` 在个别
    环境下可能为空，仅靠路径匹配会漏杀，导致数据目录锁不释放。
    """
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='mysqld.exe'\" | "
        "ForEach-Object { \"{0}|||{1}|||{2}\" -f $_.ProcessId, $_.ExecutablePath, $_.CommandLine }"
    )
    ok, output = _run(
        ["powershell", "-NoProfile", "-Command", script], timeout=_TIMEOUT_SHORT
    )
    procs = []
    if ok and output:
        for line in output.splitlines():
            pid, sep, rest = line.partition("|||")
            if not sep or not pid.strip().isdigit():
                continue
            exe, _, cmdline = rest.partition("|||")
            procs.append((pid.strip(), exe.strip(), cmdline.strip()))
    return procs


def _matching_procs(box, ver):
    """返回属于该版本的 mysqld 进程列表：按可执行路径或命令行（含版本根/服务名）匹配。"""
    try:
        root = str(_version_root(box, ver)).lower()
    except RuntimeError:
        root = ""
    name = service_name(ver).lower()
    result = []
    for pid, exe, cmdline in _list_mysqld_procs():
        blob = (exe + " " + cmdline).lower()
        if (root and root in blob) or (name and name in blob):
            result.append((pid, exe, cmdline))
    return result


def kill_version_processes(box, ver, log_cb=None):
    """结束属于该版本 ``mysqld.exe`` 的进程（按路径/命令行匹配），避免多实例端口冲突。

    仅杀路径或命令行落在该版本安装根目录/服务名下的 mysqld，不影响其他版本实例。失败静默。
    """
    for pid, _exe, _cmd in _matching_procs(box, ver):
        _run(["taskkill", "/F", "/PID", pid], timeout=_TIMEOUT_SHORT, log_cb=log_cb)


def _wait_no_mysqld(box, ver, timeout=20, log_cb=None):
    """等待该版本的 mysqld 进程全部消失（必要时反复强杀）；超时返回 False。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        procs = _matching_procs(box, ver)
        if not procs:
            return True
        for pid, _exe, _cmd in procs:
            _run(["taskkill", "/F", "/PID", pid], timeout=_TIMEOUT_SHORT, log_cb=log_cb)
        time.sleep(1)
    return not _matching_procs(box, ver)


def _file_locked(path):
    """判断文件是否被其它进程独占占用（以独占读写方式尝试打开）。

    Windows 下 mysqld 对 ibdata1 等数据文件持排它锁；若仍被占用，第二个 mysqld
    打开会失败并报 ``The innodb_system data file 'ibdata1' must be writable``。
    此处用 ``CreateFileW`` 以 ``dwShareMode=0`` 探测，失败即视为仍被占用。
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        generic_rw = 0x80000000 | 0x40000000  # GENERIC_READ | GENERIC_WRITE
        open_existing = 3
        invalid = wintypes.HANDLE(-1).value
        handle = kernel32.CreateFileW(
            str(path), generic_rw, 0, None, open_existing, 0, None
        )
        if handle == invalid or not handle:
            return True
        kernel32.CloseHandle(handle)
        return False
    except Exception:  # noqa: BLE001 - 探测失败按“未占用”处理，不阻塞主流程
        return False


def _wait_datadir_unlocked(box, ver, timeout=20, log_cb=None):
    """等待数据目录（ibdata1）的文件锁释放；期间反复清理残留 mysqld。超时返回 False。"""
    try:
        ibdata = _version_root(box, ver) / "data" / "ibdata1"
    except RuntimeError:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not ibdata.exists():
            return True
        if not _file_locked(ibdata):
            return True
        kill_version_processes(box, ver, log_cb)
        time.sleep(1)
    return not ibdata.exists() or not _file_locked(ibdata)


def _sql_escape(value):
    """转义 SQL 字符串字面量中的反斜杠与单引号（MySQL 默认允许反斜杠转义）。"""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _run_sql(box, ver, sql, user, password, log_cb=None):
    """用 mysql 客户端以指定用户执行 SQL（``-e``），返回 ``(ok, output)``。

    关键：用 ``--no-defaults`` + 显式 ``-h/-P`` 连接，**不传 ``--defaults-file``**。
    否则 mysql 客户端解析含中文的 defaults-file 路径时会因命令行编码出错而报
    ``Failed to open required defaults file``（mysqld 服务端不受影响，仅客户端）。
    端口取 ``instance_port``，与 my.ini 中一致。日志对密码做掩码，绝不回传明文。
    """
    bin_dir = _bin_dir(box, ver)
    mysql = bin_dir / "mysql.exe"
    port = instance_port(box, ver)
    args = [
        str(mysql), "--no-defaults",
        "-h", "127.0.0.1", "-P", str(port),
        "-u", user,
    ]
    if password:
        args.append("-p" + str(password))
    args += ["-e", sql]

    if log_cb:
        try:
            log_cb("> mysql --no-defaults -h 127.0.0.1 -P %d -u %s -p**** -e <SQL>" % (port, user))
        except Exception:  # noqa: BLE001
            pass
    ok, output = _run(args, cwd=bin_dir, timeout=_TIMEOUT_NORMAL, log_cb=None)
    if log_cb and output:
        try:
            log_cb(output)
        except Exception:  # noqa: BLE001
            pass
    return ok, output


def _service_state(name):
    """返回服务 STATE 数值（1=停止,4=运行），查询失败/不存在返回 None。"""
    ok, output = _run(["sc", "query", name], timeout=_TIMEOUT_SHORT)
    if not ok:
        return None
    match = re.search(r"(?:STATE|状态)\s*:\s*(\d+)", output)
    return int(match.group(1)) if match else None


def _wait_service_stopped(name, timeout=25):
    """等待服务停止（STATE=1 或不存在）；超时返回 False。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _service_state(name)
        if st is None or st == 1:
            return True
        time.sleep(1)
    return False


def _wait_service_running(name, timeout=30):
    """等待服务进入运行态（STATE=4）；超时返回 False。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _service_state(name) == 4:
            return True
        time.sleep(1)
    return False


def _tail_text(path, max_chars=600):
    """读取文件末尾若干字符（用于回传错误日志片段）；失败返回空串。"""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8", errors="replace")[-max_chars:].strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# root 密码：修改 / 重置
# --------------------------------------------------------------------------- #


def set_root_password(box, ver, new_pwd, old_pwd, log_cb=None):
    """修改 root 密码（需服务在运行且知道当前密码）：连接后执行 ``ALTER USER``。"""
    if not new_pwd:
        return {"ok": False, "message": "新密码不能为空。"}
    try:
        sql = (
            "ALTER USER 'root'@'localhost' IDENTIFIED BY '%s'; FLUSH PRIVILEGES;"
            % _sql_escape(new_pwd)
        )
        ok, output = _run_sql(box, ver, sql, "root", old_pwd or "", log_cb=log_cb)
        if ok:
            return {"ok": True, "message": "root 密码已修改。"}
        low = (output or "").lower()
        if "access denied" in low or "拒绝" in (output or ""):
            return {
                "ok": False,
                "message": "当前密码不正确（Access denied）。若已忘记密码请改用「重置密码」。",
            }
        return {"ok": False, "message": output or "修改失败，请确认服务已启动且密码正确。"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "修改密码失败：%s" % exc}


def _ensure_stopped(box, ver, name, log_cb=None):
    """停止服务并确认彻底停止、无残留 mysqld、数据目录锁已释放。

    返回 ``_wait_datadir_unlocked`` 的结果：True 表示数据目录已可写（无占用）。
    """
    _run(["net", "stop", name], timeout=_TIMEOUT_NORMAL, log_cb=log_cb)
    if not _wait_service_stopped(name, timeout=25):
        # net stop 未生效（权限/挂起）时补一次 sc stop 再等
        _run(["sc", "stop", name], timeout=_TIMEOUT_NORMAL, log_cb=log_cb)
        _wait_service_stopped(name, timeout=10)
    kill_version_processes(box, ver, log_cb)
    _wait_no_mysqld(box, ver, timeout=20, log_cb=log_cb)
    return _wait_datadir_unlocked(box, ver, timeout=20, log_cb=log_cb)


def _verify_password(box, ver, pwd, timeout=25):
    """在给定时间内尝试用 root/指定密码连接正在运行的服务；成功返回 True。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, _ = _run_sql(box, ver, "SELECT 1;", "root", pwd, log_cb=None)
        if ok:
            return True
        time.sleep(2)
    return False


def _apply_via_temp_instance(box, ver, mysqld, init_sql, new_pwd, log_cb=None):
    """未安装服务时：起临时 mysqld 执行 init-file 改密（无常驻实例，安全）。

    若临时实例因数据目录被占用而启动失败（进程退出），重试一次。返回是否成功改密。
    """
    ini = write_my_ini(box, ver)
    bin_dir = mysqld.parent
    for _attempt in range(2):
        proc = subprocess.Popen(
            [str(mysqld), "--defaults-file=%s" % str(ini),
             "--init-file=%s" % str(init_sql)],
            cwd=str(bin_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            deadline = time.time() + 40
            while time.time() < deadline:
                if proc.poll() is not None:
                    break  # 临时实例退出（端口/锁未释放），跳出后重试
                ok, _ = _run_sql(box, ver, "SELECT 1;", "root", new_pwd, log_cb=None)
                if ok:
                    return True
                time.sleep(2)
        finally:
            _terminate(proc)
        time.sleep(3)
    return False


def reset_root_password(box, ver, new_pwd, log_cb=None):
    """重置 root 密码（无需旧密码）。

    关键修复：改用「让常驻服务自身加载 init-file」的单实例方式改密，全程只有
    一个 mysqld 打开数据目录，从根本上杜绝“临时实例与常驻实例争用 ibdata1”导致的
    ``The innodb_system data file 'ibdata1' must be writable`` / DD 初始化失败 / Aborting。
    仅当该版本未安装为服务时，才回退到临时实例方式（此时没有竞争实例）。

    流程：彻底停止服务并确认数据目录解锁 -> 写入带 init-file 的 my.ini 并启动服务
    （服务启动早期以超级用户执行 ALTER USER）-> 轮询验证新密码 -> 去掉 init-file 重启
    使配置干净 -> 最终验证。会短暂中断该实例连接。
    """
    if not new_pwd:
        return {"ok": False, "message": "新密码不能为空。"}
    init_sql = None
    try:
        root = _version_root(box, ver)
        data = root / "data"
        if not (data / "mysql").is_dir():
            return {"ok": False, "message": "数据目录尚未初始化，请先执行「初始化实例」。"}
        bin_dir = _bin_dir(box, ver)
        mysqld = bin_dir / "mysqld.exe"
        name = service_name(ver)
        has_service = _service_exists(name)

        init_sql = data / "_devbox_reset.sql"
        init_sql.write_text(
            "ALTER USER 'root'@'localhost' IDENTIFIED BY '%s';\n" % _sql_escape(new_pwd),
            encoding="utf-8",
        )

        # 1) 彻底停止服务，确认无残留 mysqld 且数据目录锁释放
        if not _ensure_stopped(box, ver, name, log_cb):
            return {
                "ok": False,
                "message": "数据目录仍被占用（ibdata1 无法写入）。请关闭其它占用该版本"
                           "数据目录的 mysqld 进程后重试。",
            }

        if has_service:
            # 2A) 让常驻服务加载 init-file 改密（单实例，杜绝 ibdata1 争用）
            write_my_ini(box, ver, init_file=init_sql)
            start_service(box, ver, log_cb)
            _wait_service_running(name, timeout=40)
            applied = _verify_password(box, ver, new_pwd, timeout=30)
            # 3A) 去掉 init-file 并重启，避免每次开机重跑 ALTER USER，保持配置干净
            _ensure_stopped(box, ver, name, log_cb)
            write_my_ini(box, ver)
            start_service(box, ver, log_cb)
            _wait_service_running(name, timeout=30)
        else:
            # 2B) 未装服务：无竞争实例，起临时实例执行 init-file
            applied = _apply_via_temp_instance(box, ver, mysqld, init_sql, new_pwd, log_cb)

        # 4) 最终验证：以“能连上正在运行的服务”为准
        final_ok = applied or _verify_password(box, ver, new_pwd, timeout=15)
        if final_ok:
            return {"ok": True, "message": "root 密码已重置，服务已重启并验证新密码可连接。"}
        detail = _tail_text(data / "error.log", 600)
        return {
            "ok": False,
            "message": "重置未成功：新密码无法连接。请确认服务已停止（需以管理员身份运行）、"
                       "新密码符合服务器密码策略（长度/复杂度）。"
                       + (("\n错误日志末尾：\n" + detail) if detail else ""),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "重置密码失败：%s" % exc}
    finally:
        if init_sql is not None:
            try:
                init_sql.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# 创建用户
# --------------------------------------------------------------------------- #


def create_user(box, ver, user, host, pwd, grant_db, root_pwd, log_cb=None):
    """创建数据库用户（可选授权某个库的全部权限）。需以 root 连接执行。"""
    user = str(user or "").strip()
    if not user:
        return {"ok": False, "message": "用户名不能为空。"}
    host = str(host or "").strip() or "localhost"
    grant_db = str(grant_db or "").strip()
    if grant_db and not re.match(r"^[A-Za-z0-9_]+$", grant_db):
        return {"ok": False, "message": "授权的数据库名只允许字母、数字和下划线。"}
    try:
        u = _sql_escape(user)
        h = _sql_escape(host)
        p = _sql_escape(pwd or "")
        sql = "CREATE USER IF NOT EXISTS '%s'@'%s' IDENTIFIED BY '%s';" % (u, h, p)
        if grant_db:
            sql += " GRANT ALL PRIVILEGES ON `%s`.* TO '%s'@'%s';" % (grant_db, u, h)
        sql += " FLUSH PRIVILEGES;"
        ok, output = _run_sql(box, ver, sql, "root", root_pwd or "", log_cb=log_cb)
        if ok:
            tail = ("，并授权库 %s" % grant_db) if grant_db else ""
            return {"ok": True, "message": "用户 %s@%s 已创建%s。" % (user, host, tail)}
        low = (output or "").lower()
        if "access denied" in low or "拒绝" in (output or ""):
            return {"ok": False, "message": "root 密码不正确（Access denied），无法执行创建。"}
        return {"ok": False, "message": output or "创建用户失败，请确认服务已启动。"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "创建用户失败：%s" % exc}


# --------------------------------------------------------------------------- #
# 版本切换：停旧启新，避免冲突
# --------------------------------------------------------------------------- #


def switch_active(box, old_ver, new_ver, log_cb=None):
    """切换当前 MySQL 版本：停止旧版本服务并清理残留进程，若新版本已装服务则启动。

    返回 ``{'ok', 'message'}``。
    """
    try:
        if old_ver and old_ver != new_ver:
            stop_service(box, old_ver, log_cb)
            kill_version_processes(box, old_ver, log_cb)
        if _service_exists(service_name(new_ver)):
            res = start_service(box, new_ver, log_cb)
            return {
                "ok": bool(res.get("ok")),
                "message": "已切换 MySQL 服务 %s -> %s：%s"
                % (old_ver, new_ver, res.get("message", "")),
            }
        return {
            "ok": True,
            "message": "已停止旧版本 MySQL 服务（新版本 %s 尚未安装为服务，未自动启动）。" % new_ver,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "切换 MySQL 服务失败：%s" % exc}