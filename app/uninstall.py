# -*- coding: utf-8 -*-
"""DevBox 版本卸载与残留清理。

为「已安装版本」提供跨工具（java/node/python/php/maven/mysql/redis）的统一卸载能力，
并在卸载前扫描、卸载时清理各类**残留**，避免留下孤立服务、进程、目录与缓存：

- **Windows 服务**：MySQL / Redis 按版本注册的服务（``MySQL80_*`` / ``Redis_*``）；
- **运行中进程**：mysqld.exe / redis-server.exe 中属于该版本的实例；
- **版本安装目录**：``<runtime_dir>/<tool>/<ver>``（含 MySQL 的 data 子目录）；
- **实例配置目录**：``<runtime_dir>/mysql/<ver>``、``<runtime_dir>/redis/<ver>``；
- **下载缓存**：``<cache_dir>`` 下文件名含版本号的 zip；
- **激活标记**：若被卸载版本是当前激活版本，清除 current 并交由上层重写 shim；
- **用户环境变量（C 盘注册表）**：``*_HOME`` 写在 ``HKCU\\Environment``（位于 C 盘），
  卸载当前版本后由 ``env.sync_homes`` 清理指向已删目录的失效项。

安全约定：
- 来源为 ``system`` 的记录**只移除 DevBox 登记**，绝不删除系统安装目录、
  也绝不卸载/停止系统自带服务；
- 所有删除操作均 ``ignore_errors`` / ``try-except`` 包裹，单项失败不影响整体；
- 不自动提权：服务卸载需要管理员权限，失败时如实返回输出。
"""

import shutil
from pathlib import Path

from app import env, mysqlsvc, redissvc


def _instance_dir(box, tool, ver):
    """返回 MySQL / Redis 的实例配置目录；其他工具返回 None。"""
    if tool == "mysql":
        return mysqlsvc.instance_dir(box, ver)
    if tool == "redis":
        return redissvc.instance_dir(box, ver)
    return None


def _cache_zips(box, ver):
    """返回缓存目录中文件名含版本号的 zip 列表（疑似该版本下载缓存）。"""
    result = []
    cache = Path(box.data.get("cache_dir", ""))
    ver = str(ver)
    if not ver or not cache.is_dir():
        return result
    try:
        for item in cache.iterdir():
            if item.is_file() and item.suffix.lower() == ".zip" and ver in item.name:
                result.append(item)
    except OSError:
        return []
    return result


def scan_residuals(box, tool, ver):
    """扫描该版本的残留项，返回 ``(items, is_system)``。

    ``items`` 为 ``[(类别, 详情), ...]``，用于卸载前向用户展示将被清理的内容；
    ``is_system`` 为 True 表示记录来自系统安装（仅移除登记，不删目录/服务）。
    """
    record = box.find_installed(tool, ver)
    is_system = bool(record) and record.get("source") == "system"
    items = []

    # 1) Windows 服务与运行中进程（仅 DevBox 管理的 mysql/redis）
    if tool == "mysql" and not is_system:
        if mysqlsvc.service_installed(box, ver):
            items.append(("Windows 服务", mysqlsvc.service_name(ver)))
        procs = mysqlsvc.running_processes(box, ver)
        if procs:
            items.append(("运行中进程", "%d 个 mysqld" % len(procs)))
    elif tool == "redis" and not is_system:
        if redissvc.service_installed(box, ver):
            items.append(("Windows 服务", redissvc.service_name(ver)))
        procs = redissvc.running_processes(box, ver)
        if procs:
            items.append(("运行中进程", "%d 个 redis-server" % len(procs)))

    # 2) 版本安装目录（system 来源不删）
    if record and record.get("path") and not is_system:
        if Path(record["path"]).exists():
            items.append(("版本安装目录", record["path"]))

    # 3) 实例配置目录（mysql/redis）
    idir = _instance_dir(box, tool, ver)
    if idir is not None and idir.exists():
        items.append(("实例配置目录", str(idir)))

    # 4) 下载缓存
    for zip_path in _cache_zips(box, ver):
        items.append(("下载缓存", zip_path.name))

    # 5) 激活标记与用户级环境变量（*_HOME 指向被删版本时一并清理，注册表位于 C 盘）
    if box.current(tool) == ver:
        detail = "卸载后将清除激活标记并重新生成 shim"
        env_name = env.ENV_MAP.get(tool)
        if env_name:
            runtime_lower = str(box.data.get("runtime_dir", "")).rstrip("\\/").lower()
            cur_val = env.read_env(env_name)
            if cur_val and runtime_lower and cur_val.rstrip("\\/").lower().startswith(runtime_lower):
                detail += "；并清理用户环境变量 %s（%s）" % (env_name, cur_val)
        items.append(("当前激活版本", detail))

    return items, is_system


def uninstall(box, tool, ver, log_cb=None):
    """彻底卸载指定版本并清理残留，返回 ``{'ok', 'message', 'was_current'}``。

    流程：停服务/卸服务 -> 杀残留进程 -> 删版本目录 -> 删实例配置目录 ->
    删下载缓存 -> 移除登记（若为当前版本则清除 current）。
    shim / 环境变量的重写由上层在卸载成功后调用 ``write_shims`` / ``sync_homes``。
    """
    def log(text):
        if log_cb:
            try:
                log_cb(text)
            except Exception:  # noqa: BLE001
                pass

    record = box.find_installed(tool, ver)
    is_system = bool(record) and record.get("source") == "system"
    was_current = box.current(tool) == ver
    removed = []
    warnings = []

    try:
        if not is_system:
            # 1) 服务与进程
            if tool == "mysql":
                if mysqlsvc.service_installed(box, ver):
                    log("卸载 Windows 服务 %s ..." % mysqlsvc.service_name(ver))
                    res = mysqlsvc.uninstall_service(box, ver, log_cb)
                    if res.get("ok"):
                        removed.append("服务 " + mysqlsvc.service_name(ver))
                    else:
                        warnings.append("服务卸载失败：%s" % res.get("message", ""))
                mysqlsvc.kill_version_processes(box, ver, log_cb)
            elif tool == "redis":
                if redissvc.service_installed(box, ver):
                    log("卸载 Windows 服务 %s ..." % redissvc.service_name(ver))
                    res = redissvc.uninstall_service(box, ver, log_cb)
                    if res.get("ok"):
                        removed.append("服务 " + redissvc.service_name(ver))
                    else:
                        warnings.append("服务卸载失败：%s" % res.get("message", ""))
                redissvc.kill_version_processes(box, ver, log_cb)

            # 2) 版本安装目录
            if record and record.get("path"):
                target = Path(record["path"])
                if target.exists():
                    log("删除版本安装目录 %s ..." % target)
                    shutil.rmtree(str(target), ignore_errors=True)
                    if not target.exists():
                        removed.append("安装目录")
                    else:
                        warnings.append("安装目录未能完全删除：%s" % target)

            # 3) 实例配置目录
            idir = _instance_dir(box, tool, ver)
            if idir is not None and idir.exists():
                log("删除实例配置目录 %s ..." % idir)
                shutil.rmtree(str(idir), ignore_errors=True)
                if not idir.exists():
                    removed.append("实例配置")

            # 4) 下载缓存
            for zip_path in _cache_zips(box, ver):
                try:
                    zip_path.unlink()
                    removed.append("缓存 " + zip_path.name)
                except OSError:
                    warnings.append("缓存删除失败：%s" % zip_path.name)

            # 4.5) 若该工具的运行时目录已空（最后一个版本已卸），一并移除空的父目录
            try:
                tool_dir = Path(box.data["runtime_dir"]) / tool
                if tool_dir.is_dir() and not any(tool_dir.iterdir()):
                    tool_dir.rmdir()
                    removed.append("空工具目录")
            except OSError:
                pass
        else:
            log("来源为系统安装：仅移除 DevBox 登记，不删除系统目录/服务。")

        # 5) 移除登记与激活标记
        box.remove_installed(tool, ver)
        if was_current:
            box.data["current"].pop(tool, None)
            box.save()
        removed.append("版本登记")

        message = "已卸载 %s %s。" % (tool, ver)
        if removed:
            message += " 清理：%s。" % "、".join(removed)
        if warnings:
            message += " 注意：%s" % "；".join(warnings)
        return {"ok": True, "message": message, "was_current": was_current}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "卸载失败：%s" % exc, "was_current": was_current}
