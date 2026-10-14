# -*- coding: utf-8 -*-
"""Redis 服务端实例管理（Windows 服务化，多版本共存）。

设计完全对齐 :mod:`app.mysqlsvc` 的多实例思路：

1. **客户端版本切换**：由既有 Shim 机制完成，``redis-cli.exe`` / ``redis-server.exe``
   始终来自当前选中版本的根目录，本模块不负责。
2. **服务端实例管理**：本模块负责。Windows 版 Redis（tporadowski / MicrosoftArchive
   发行版）自带服务化能力，为了让多个 Redis 版本共存，按版本创建相互独立的实例：

   - 每个版本使用独立的 **配置目录**（``<runtime_dir>/redis/<ver>``）存放 redis.conf；
   - 每个版本使用独立的 **端口**（6379 起按排序依次递增）；
   - 每个版本使用独立的 **数据/日志目录**（``<版本根目录>``）；
   - 每个版本注册为 **唯一服务名**（``Redis_<版本号下划线化>``）。

   由此可以安装服务、启动、停止、卸载，并设置 **内存上限**（``maxmemory``）与
   **访问密码**（``requirepass``）。

设计约定：
- 仅依赖 Python 标准库（``re`` / ``subprocess`` / ``time`` / ``pathlib``）。
- 所有外部命令调用都带 ``capture_output`` / ``text`` / 容错编码（GBK）与 ``timeout``，
  并用 ``try/except`` 包裹，异常统一转成 ``{'ok': False, 'message': ...}``。
- 配置中的路径统一使用正斜杠，避免反斜杠被转义。
- 不自动提权：涉及管理员权限的命令失败时，如实把 redis-server/sc/net 的输出返回。
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
    record = box.find_installed("redis", ver)
    if not record or not record.get("path"):
        raise RuntimeError("未找到 Redis %s 的安装记录，请先安装该版本。" % ver)
    return Path(record["path"])


def _server_exe(box, ver):
    """返回该版本的 redis-server.exe 路径（Windows 版 exe 位于版本根目录）。"""
    return PROVIDERS["redis"].bin_path(_version_root(box, ver)) / "redis-server.exe"


def _run(cmd, cwd=None, timeout=_TIMEOUT_NORMAL, log_cb=None):
    """执行外部命令，返回 ``(ok, output)``；任何异常都转成 ``(False, 文本)``。"""
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


# --------------------------------------------------------------------------- #
# 命名 / 端口 / 目录 / 配置
# --------------------------------------------------------------------------- #


def service_name(ver):
    """返回该版本的唯一 Windows 服务名，形如 ``Redis_5_0_14_1``（ver='5.0.14.1'）。"""
    return "Redis_" + str(ver).replace(".", "_")


def instance_port(box, ver):
    """为该版本分配独立端口：``6379 + 排序下标``（第一个 6379，第二个 6380，依此类推）。"""
    vers = sorted(str(r.get("ver", "")) for r in box.installed_list("redis"))
    ver = str(ver)
    if ver in vers:
        index = vers.index(ver)
    else:
        index = len(vers)
    return 6379 + index


def instance_dir(box, ver):
    """返回该版本的实例配置目录：``<runtime_dir>/redis/<ver>``。"""
    return Path(box.data["runtime_dir"]) / "redis" / str(ver)


def conf_path(box, ver):
    """返回该版本受管配置文件路径：``<实例目录>/redis.conf``。"""
    return instance_dir(box, ver) / "redis.conf"


def _parse_conf(path):
    """解析 redis.conf 为 ``{key: value}``（忽略注释与空行，后值覆盖前值）。"""
    result = {}
    try:
        p = Path(path)
        if not p.exists():
            return result
        for line in p.read_text(encoding="gbk", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                result[parts[0]] = parts[1].strip()
            elif len(parts) == 1:
                result[parts[0]] = ""
    except OSError:
        return {}
    return result


def read_maxmemory_mb(box, ver):
    """读取该版本配置的内存上限（MB）；未设置返回 None。"""
    value = _parse_conf(conf_path(box, ver)).get("maxmemory")
    if not value:
        return None
    try:
        return int(round(int(value) / (1024.0 * 1024.0)))
    except (TypeError, ValueError):
        return None


def read_password(box, ver):
    """读取该版本配置的访问密码；未设置返回空串。"""
    return _parse_conf(conf_path(box, ver)).get("requirepass", "") or ""


def write_conf(box, ver, maxmemory_mb=None, password=None):
    """在实例目录生成（覆盖写入）redis.conf，返回该 conf 的 Path。

    ``maxmemory_mb`` / ``password`` 传 None 表示沿用现有配置中的值；
    传具体值则覆盖。端口取 ``instance_port``，数据/日志目录取版本根目录。
    路径统一使用正斜杠。

    关键：必须用 GBK 写入（与 my.ini 同理）。Windows 版 redis-server 按系统 ANSI
    代码页(936/GBK) 读取 conf 中的 dir/logfile 路径；若用 UTF-8，含中文的路径会被
    误解码成乱码，启动时报 ``Could not open logfile ...: 系统找不到指定的路径``。
    因此头部注释也用 ASCII，保证除真实路径外整份 conf 为纯 ASCII。
    """
    root = _version_root(box, ver)
    root_fwd = str(root).replace("\\", "/")
    port = instance_port(box, ver)

    existing = _parse_conf(conf_path(box, ver))
    if maxmemory_mb is None:
        cur = existing.get("maxmemory")
        if cur:
            try:
                maxmemory_mb = int(round(int(cur) / (1024.0 * 1024.0)))
            except (TypeError, ValueError):
                maxmemory_mb = None
    if password is None:
        password = existing.get("requirepass", "") or ""

    conf = conf_path(box, ver)
    conf.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# DevBox managed Redis instance config (per-version port/dir)",
        "port %d" % port,
        "bind 127.0.0.1",
        "protected-mode yes",
        "dir %s" % root_fwd,
        "logfile %s/redis-server.log" % root_fwd,
        "daemonize no",
    ]
    if maxmemory_mb:
        lines.append("maxmemory %d" % (int(maxmemory_mb) * 1024 * 1024))
        lines.append("maxmemory-policy allkeys-lru")
    if password:
        lines.append("requirepass %s" % password)
    conf.write_text("\n".join(lines) + "\n", encoding="gbk")
    return conf


# --------------------------------------------------------------------------- #
# 服务生命周期
# --------------------------------------------------------------------------- #


def _service_exists(name):
    """判断 Windows 服务是否已安装（sc query 返回 1060 表示不存在）。"""
    ok, output = _run(["sc", "query", name], timeout=_TIMEOUT_SHORT)
    if ok:
        return True
    low = (output or "").lower()
    return not ("1060" in low or "does not exist" in low or "不存在" in output)


def _service_state(name):
    """返回服务 STATE 数值（1=停止,4=运行），查询失败/不存在返回 None。"""
    ok, output = _run(["sc", "query", name], timeout=_TIMEOUT_SHORT)
    if not ok:
        return None
    match = re.search(r"(?:STATE|状态)\s*:\s*(\d+)", output)
    return int(match.group(1)) if match else None


def _wait_service_stopped(name, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _service_state(name)
        if st is None or st == 1:
            return True
        time.sleep(1)
    return False


def _wait_service_running(name, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _service_state(name) == 4:
            return True
        time.sleep(1)
    return False


def install_service(box, ver, log_cb=None):
    """安装 Windows 服务：``redis-server --service-install <conf> --service-name <名>``。

    需要管理员权限；失败时把 redis-server 的输出作为 message 返回。
    """
    try:
        conf = write_conf(box, ver)
        root = _version_root(box, ver)
        server = _server_exe(box, ver)
        name = service_name(ver)
        cmd = [
            str(server), "--service-install", str(conf),
            "--service-name", name, "--loglevel", "verbose",
        ]
        ok, output = _run(cmd, cwd=root, timeout=_TIMEOUT_NORMAL, log_cb=log_cb)
        if ok:
            return {"ok": True, "message": "服务 %s 安装成功。" % name}
        # 该 build 安装失败时不向控制台输出内容；若服务其实已存在，
        # 明确提示“已存在”而非误导用户去排查管理员权限。
        if _service_exists(name):
            return {"ok": True, "message": "服务 %s 已存在，无需重复安装。" % name}
        return {
            "ok": False,
            "message": output or "服务安装失败，请确认以管理员身份运行。",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "服务安装失败：%s" % exc}


def start_service(box, ver, log_cb=None):
    """启动服务：优先 ``redis-server --service-start``，失败回退 ``net start``。"""
    name = service_name(ver)
    try:
        server = _server_exe(box, ver)
        ok, output = _run(
            [str(server), "--service-start", "--service-name", name],
            cwd=_version_root(box, ver),
            timeout=_TIMEOUT_NORMAL,
            log_cb=log_cb,
        )
        if ok:
            return {"ok": True, "message": "服务 %s 已启动。" % name}
        if "successfully" in output.lower() or "已经启动" in output:
            return {"ok": True, "message": "服务 %s 已经在运行。" % name}

        ok2, output2 = _run(["net", "start", name], timeout=_TIMEOUT_NORMAL, log_cb=log_cb)
        if ok2:
            return {"ok": True, "message": "服务 %s 已启动（net start）。" % name}
        return {
            "ok": False,
            "message": output2 or output or "启动服务失败，请确认以管理员身份运行。",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "启动服务失败：%s" % exc}


def stop_service(box, ver, log_cb=None):
    """停止服务：``redis-server --service-stop``，失败回退 ``net stop``。"""
    name = service_name(ver)
    try:
        server = _server_exe(box, ver)
        ok, output = _run(
            [str(server), "--service-stop", "--service-name", name],
            cwd=_version_root(box, ver),
            timeout=_TIMEOUT_NORMAL,
            log_cb=log_cb,
        )
        if ok:
            return {"ok": True, "message": "服务 %s 已停止。" % name}
        if "未启动" in output or "not started" in output.lower():
            return {"ok": True, "message": "服务 %s 当前未运行。" % name}

        ok2, output2 = _run(["net", "stop", name], timeout=_TIMEOUT_NORMAL, log_cb=log_cb)
        if ok2:
            return {"ok": True, "message": "服务 %s 已停止（net stop）。" % name}
        return {
            "ok": False,
            "message": output2 or output or "停止服务失败，请确认以管理员身份运行。",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "停止服务失败：%s" % exc}


def uninstall_service(box, ver, log_cb=None):
    """卸载服务：先停止，再 ``--service-uninstall``，失败回退 ``sc delete``。"""
    name = service_name(ver)
    try:
        _run(["net", "stop", name], timeout=_TIMEOUT_NORMAL, log_cb=log_cb)
        server = _server_exe(box, ver)
        ok, output = _run(
            [str(server), "--service-uninstall", "--service-name", name],
            cwd=_version_root(box, ver),
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
    except Exception:  # noqa: BLE001
        return "未安装"
    if not ok:
        return "未安装"
    match = re.search(r"(?:STATE|状态)\s*:\s*(\d+)", output)
    if match:
        return _STATE_TEXT.get(int(match.group(1)), "未知")
    return "未知"


def service_installed(box, ver):
    """判断该版本的 Windows 服务是否已安装（供卸载残留检查使用）。"""
    return _service_exists(service_name(ver))


def running_processes(box, ver):
    """返回属于该版本的运行中 redis-server 进程列表（供卸载残留检查使用）。"""
    return _matching_procs(box, ver)


def _restart_service(box, ver, log_cb=None):
    """停止并重新启动服务（用于让配置变更生效）。"""
    stop_service(box, ver, log_cb)
    _wait_service_stopped(service_name(ver), timeout=20)
    kill_version_processes(box, ver, log_cb)
    res = start_service(box, ver, log_cb)
    _wait_service_running(service_name(ver), timeout=30)
    return res


# --------------------------------------------------------------------------- #
# 内存上限 / 访问密码
# --------------------------------------------------------------------------- #


def set_maxmemory(box, ver, maxmemory_mb, log_cb=None):
    """设置内存上限（MB）：写入 conf 后重启服务生效。"""
    try:
        mb = int(str(maxmemory_mb).strip())
    except (TypeError, ValueError):
        return {"ok": False, "message": "内存大小必须为正整数（单位 MB）。"}
    if mb <= 0:
        return {"ok": False, "message": "内存大小必须为正整数（单位 MB）。"}
    try:
        write_conf(box, ver, maxmemory_mb=mb, password=None)
        res = _restart_service(box, ver, log_cb)
        return {
            "ok": bool(res.get("ok")),
            "message": "内存上限已设置为 %d MB 并重启服务。%s"
                       % (mb, res.get("message", "")),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "设置内存上限失败：%s" % exc}


def set_password(box, ver, password, log_cb=None):
    """设置访问密码（requirepass）：写入 conf 后重启服务生效；传空串表示清除密码。"""
    password = str(password or "")
    try:
        write_conf(box, ver, maxmemory_mb=None, password=password)
        res = _restart_service(box, ver, log_cb)
        action = "已设置" if password else "已清除"
        return {
            "ok": bool(res.get("ok")),
            "message": "访问密码%s并重启服务。%s" % (action, res.get("message", "")),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "设置访问密码失败：%s" % exc}


# --------------------------------------------------------------------------- #
# 进程管理 / 版本切换
# --------------------------------------------------------------------------- #


def _list_redis_procs():
    """通过 PowerShell 枚举当前运行的 redis-server.exe，返回 ``[(pid, exe, cmdline), ...]``。"""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='redis-server.exe'\" | "
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
    """返回属于该版本的 redis-server 进程列表：按路径或命令行（含版本根/服务名/端口）匹配。"""
    try:
        root = str(_version_root(box, ver)).lower()
    except RuntimeError:
        root = ""
    name = service_name(ver).lower()
    port = str(instance_port(box, ver))
    result = []
    for pid, exe, cmdline in _list_redis_procs():
        blob = (exe + " " + cmdline).lower()
        if (root and root in blob) or (name and name in blob) or (":" + port in blob):
            result.append((pid, exe, cmdline))
    return result


def kill_version_processes(box, ver, log_cb=None):
    """结束属于该版本的 redis-server 进程，避免多实例端口冲突。失败静默。"""
    for pid, _exe, _cmd in _matching_procs(box, ver):
        _run(["taskkill", "/F", "/PID", pid], timeout=_TIMEOUT_SHORT, log_cb=log_cb)


def switch_active(box, old_ver, new_ver, log_cb=None):
    """切换当前 Redis 版本：停止旧版本服务并清理残留进程，若新版本已装服务则启动。"""
    try:
        if old_ver and old_ver != new_ver:
            stop_service(box, old_ver, log_cb)
            kill_version_processes(box, old_ver, log_cb)
        if _service_exists(service_name(new_ver)):
            res = start_service(box, new_ver, log_cb)
            return {
                "ok": bool(res.get("ok")),
                "message": "已切换 Redis 服务 %s -> %s：%s"
                           % (old_ver, new_ver, res.get("message", "")),
            }
        return {
            "ok": True,
            "message": "已停止旧版本 Redis 服务（新版本 %s 尚未安装为服务，未自动启动）。" % new_ver,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "切换 Redis 服务失败：%s" % exc}
