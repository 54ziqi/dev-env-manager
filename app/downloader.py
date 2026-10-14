"""DevBox 下载与解压层。

提供安全、可续传、可重试、防路径遍历的 zip 下载与解压能力：
- download()：以 64KB 分块下载到目标文件，支持进度回调、Range 断点续传与自动重试；
- unpack()：解压前整体校验成员路径，拒绝路径遍历，损坏 zip 抛出友好异常。

仅依赖 Python 标准库。
"""

import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

# 下载分块大小：64KB
_CHUNK_SIZE = 65536
# 网络超时：不小于 120 秒
_TIMEOUT = 120
# 最大尝试次数（含首次）
_MAX_ATTEMPTS = 3
# 重试间隔（秒）
_RETRY_DELAY = 1

# 请求头，部分服务端会拒绝无 UA 的请求
_HEADERS = {
    "User-Agent": "DevBox/1.0",
}


def download(url, dest, cb=None):
    """分块下载 url 到 dest 路径（dest 是文件完整路径，含文件名）。

    - 以 64KB（65536 字节）分块读取写入；
    - cb 为可选进度回调，签名为 cb(downloaded_bytes: int, total_bytes: int)；
      total 未知（服务端未返回 Content-Length）时传 0；
    - 目标文件已存在时，发送 Range 请求头从断点续传（用已存在文件大小作为起始偏移）；
      若服务端不支持（返回状态非 206），则退化为从头覆盖下载（以 'wb' 重新开始）；
    - timeout 不小于 120 秒；
    - 网络异常自动重试，最多 3 次（含首次共 3 次尝试）；
    - 全部失败时抛出异常；
    - 确保 dest 的父目录存在；
    - 返回 dest 的 pathlib.Path。

    注意：进度回调中下载完成时会回调一次 cb(total, total)。
    """
    dest = Path(dest)
    # 确保父目录存在
    dest.parent.mkdir(parents=True, exist_ok=True)

    last_error = None
    # 在整个"打开连接 + 分块写入"过程外层做重试
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return _download_once(url, dest, cb)
        except Exception as exc:  # 网络/IO 等异常统一重试
            last_error = exc
            # 未到最大次数则等待后重试；已下载字节数在重试间保留（依赖 Range 续传）
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAY)

    # 全部失败：抛出异常并带上原始错误信息
    raise RuntimeError("下载失败（已重试 %d 次）：%s" % (_MAX_ATTEMPTS, last_error))


def download_any(urls, dest, cb=None):
    """依次尝试多个候选 URL，任一成功即返回；全部失败抛出汇总异常。

    - 用于国内镜像优先、官方兜底的多源下载；
    - 同一 URL 的重试由 download() 内部完成；切换到下一个源前删除可能的
      半成品，避免跨源断点续传导致文件不一致（不同源内容应相同，但为安全起见重置）。
    """
    urls = [u for u in (urls or []) if u]
    if not urls:
        raise RuntimeError("没有可用的下载地址。")

    errors = []
    for index, url in enumerate(urls):
        try:
            return download(url, dest, cb)
        except Exception as exc:  # noqa: BLE001 - 单个源失败则尝试下一个
            errors.append("[%d] %s\n     原因：%s" % (index + 1, url, exc))
            _discard_partial(dest)

    raise RuntimeError("所有下载源均失败（共 %d 个）：\n%s" % (len(urls), "\n".join(errors)))


def _discard_partial(dest):
    """删除可能不完整的下载文件，为切换下一个源作准备（失败忽略）。"""
    try:
        if Path(dest).exists():
            Path(dest).unlink()
    except OSError:
        pass


def _download_once(url, dest, cb):
    """执行一次完整的下载尝试，返回 dest 的 Path，失败则向上抛出异常。"""
    # 判断是否存在可续传的部分文件（大小 > 0）
    resume_from = 0
    if dest.exists():
        try:
            size = dest.stat().st_size
            if size > 0:
                resume_from = size
        except OSError:
            resume_from = 0

    headers = dict(_HEADERS)
    mode = "wb"
    if resume_from > 0:
        headers["Range"] = "bytes=%d-" % resume_from
        mode = "ab"

    request = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(request, timeout=_TIMEOUT)
    except urllib.error.HTTPError as exc:
        # 416 = 请求的 Range 不可满足：当本地文件已完整时（偏移==总大小）服务端会返回此状态，
        # 视为下载已完成，直接返回，避免不必要的重下或 416 异常。
        if resume_from > 0 and exc.code == 416:
            if cb:
                cb(resume_from, resume_from)
            return dest
        raise
    with resp:
        status = getattr(resp, "status", None) or resp.getcode()
        # 服务端不支持断点续传（返回非 206）时，清空文件从头下载，避免数据错乱
        if resume_from > 0 and status != 206:
            mode = "wb"
            downloaded = 0
            total = _content_length(resp)
        else:
            downloaded = resume_from
            # 206 时 Content-Length 是本次剩余长度，需叠加已下载部分还原总大小
            total = _content_length(resp)
            if total is not None:
                total += resume_from

        with open(dest, mode) as f:
            while True:
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if cb:
                    cb(downloaded, total if total is not None else 0)

    # 下载完成：回调一次完成进度（total 未知时用已下载字节数兜底）
    if cb:
        final_total = total if total is not None else downloaded
        cb(final_total, final_total)

    return dest


def _content_length(resp):
    """从响应头解析 Content-Length，缺失或非法时返回 None。"""
    value = resp.headers.get("Content-Length")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def unpack(zip_path, target_dir):
    """解压 zip_path 到 target_dir。

    - 解压前遍历 zipfile 成员，校验成员路径安全：使用 PurePosixPath / PureWindowsPath
      规范化后，拒绝包含 '..' 或绝对路径（如以 '/' 或盘符开头）的成员，非法则抛出
      ValueError，且不得解压任何文件（先整体校验再解压）；
    - 校验通过后用 zipfile 解压到 target_dir（target_dir 不存在则创建）；
    - zip 损坏/非法（zipfile.BadZipFile）时抛出带友好中文提示的异常。
    """
    zip_path = Path(zip_path)
    target_dir = Path(target_dir)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            # 先整体校验所有成员路径，任一非法立即中止，不解压任何文件
            for info in zf.infolist():
                _check_member(info.filename)

            # 校验通过后再创建目录并解压
            target_dir.mkdir(parents=True, exist_ok=True)
            zf.extractall(target_dir)
    except zipfile.BadZipFile as exc:
        raise ValueError(
            "压缩包损坏或不是合法的 zip 文件: %s (%s)" % (zip_path, exc)
        )


def _check_member(filename):
    """校验单个 zip 成员路径是否安全，非法则抛出 ValueError。

    规则：路径规范化后不得为绝对路径（'/' 开头、含盘符），且任一分段不得为 '..'。
    """
    # 统一分隔符后用 POSIX 语义判断，兼顾 Windows 反斜杠写法
    normalized = filename.replace("\\", "/")
    posix = PurePosixPath(normalized)

    # 绝对路径：POSIX 以 '/' 开头，或 Windows 带盘符（如 C:...）
    if posix.is_absolute() or PureWindowsPath(filename).is_absolute():
        raise ValueError("压缩包包含非法成员（绝对路径）：%s" % filename)
    # 形如 "C:foo" 这类带盘符但非绝对路径的写法也要拦截
    if len(filename) >= 2 and filename[1:2] == ":":
        raise ValueError("压缩包包含非法成员（含盘符）：%s" % filename)

    # 路径分段中出现 '..' 即为路径遍历
    if ".." in posix.parts:
        raise ValueError("压缩包包含非法成员（路径遍历）：%s" % filename)


def validate_zip_package(zip_path, markers=None):
    """校验 zip 压缩包：合法性 + （可选）包含关键文件。

    - 先确认文件存在且为合法 zip（损坏/非 zip 抛 ValueError）；
    - markers 为相对路径后缀列表（如 ["bin/java.exe"]），逐个校验压缩包内
      是否存在以该后缀结尾的成员（忽略大小写与分隔符）；任一缺失抛 ValueError，
      提示“可能不是对应的安装包”；markers 为空则仅做 zip 合法性校验。
    - 返回成员名列表（已规范化为小写、正斜杠）。
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise ValueError("文件不存在：%s" % zip_path)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [info.filename.replace("\\", "/").lower() for info in zf.infolist()]
    except zipfile.BadZipFile as exc:
        raise ValueError("压缩包损坏或不是合法的 zip 文件：%s (%s)" % (zip_path, exc))
    except OSError as exc:
        raise ValueError("无法读取压缩包：%s (%s)" % (zip_path, exc))

    if not names:
        raise ValueError("压缩包为空：%s" % zip_path)

    for marker in markers or []:
        m = marker.replace("\\", "/").lower().lstrip("/")
        if not any(n == m or n.endswith("/" + m) for n in names):
            raise ValueError(
                "该压缩包内未找到关键文件 %s，可能不是对应的安装包或文件不完整。" % marker
            )
    return names
