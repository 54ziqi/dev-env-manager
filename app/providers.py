"""DevBox 版本源适配器（Provider）层。

为 Java、Node.js、Python、PHP、Maven、MySQL 六种开发环境提供统一的版本源接口：

- ``list_versions``：列出可下载版本（在线源或静态版本表）。
- ``build_url``：根据版本号构造下载 URL。
- ``bin_path``：推导可执行文件所在目录。
- ``post_install``：解压后的目录/配置后处理。
- ``scan_installed``：扫描已安装版本。

仅依赖 Python 标准库；所有网络访问均设置 ``User-Agent`` 与 ``timeout=120``，
所有解析/文件操作均做防御性处理，任何异常都不得让上层崩溃。
"""

import glob
import json
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path

# 统一请求头：部分版本源会拒绝无 User-Agent 的请求。
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DevBox/1.0"

# 统一网络超时时间（秒）。
_TIMEOUT = 120


def _prefer_cn(mirror):
    """判断下载源偏好是否为“国内镜像优先”。"""
    return str(mirror or "").strip().lower() in ("cn", "mirror", "domestic", "tsinghua", "npmmirror")


def _dedup_urls(urls):
    """去除空值与重复项，保留原始顺序。"""
    seen = set()
    result = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            result.append(u)
    return result


def _adoptium_mirrors(official):
    """把 Adoptium 官方 GitHub release 直链改写为清华 TUNA 的 Adoptium 镜像直链。

    官方形如：``https://github.com/adoptium/temurin17-binaries/releases/download/.../OpenJDK17U-jdk_x64_windows_hotspot_17.0.11_9.zip``
    镜像：``https://mirrors.tuna.tsinghua.edu.cn/Adoptium/17/jdk/x64/windows/<同名 zip>``
    无法改写（非 GitHub 链接或非 zip）时返回空列表（自然回退官方）。
    """
    if not official or "github.com" not in official or not official.lower().endswith(".zip"):
        return []
    filename = official.rsplit("/", 1)[-1]
    match = re.search(r"OpenJDK(\d+)", filename)
    if not match:
        return []
    major = match.group(1)
    return [
        "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/%s/jdk/x64/windows/%s" % (major, filename),
    ]


def _http_get(url):
    """发起 GET 请求并返回响应体 bytes；失败（网络/超时/HTTP 错误）返回 None。"""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json, */*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        # HTTPError 是 URLError 的子类，此处一并捕获；任何网络异常都返回 None
        return None


class Provider:
    """版本源适配器基类。

    子类通过类属性声明工具名、可执行文件列表与可执行文件所在子目录，
    并按需重写 ``list_versions`` / ``build_url`` / ``post_install``。
    """

    name = ""          # 工具名，如 "java"
    exe_names = []     # 可执行文件列表
    bin_sub = ""       # 可执行文件相对版本根目录的子目录；空串表示就在根目录
    # 安装包关键文件（相对路径后缀）：导入本地 zip 时用于校验“是否为对应安装包”。
    # 空列表表示不做文件校验（仅校验压缩包合法性）。
    install_markers = []

    def list_versions(self, query=None):
        """返回 ``[{"ver", "url", "size"}, ...]``；``query`` 为 None 或版本关键字。

        基类默认返回空列表，由子类覆盖实现。
        """
        return []

    def build_url(self, ver):
        """根据版本号构造下载 URL；无法构造时返回空串。"""
        return ""

    def mirror_urls(self, ver, official=None):
        """返回该版本的国内镜像候选 URL 列表（默认无镜像，由子类覆盖）。"""
        return []

    def build_urls(self, ver, mirror="cn"):
        """返回按优先级排序的下载候选 URL（已去重）。

        - mirror 为国内镜像偏好时：镜像在前、官方在后（镜像失败自动回退官方）；
        - 否则：官方在前、镜像在后作为兜底。
        """
        official = self.build_url(ver)
        mirrors = [u for u in self.mirror_urls(ver, official) if u]
        if _prefer_cn(mirror):
            ordered = mirrors + ([official] if official else [])
        else:
            ordered = ([official] if official else []) + mirrors
        return _dedup_urls(ordered)

    def bin_path(self, root):
        """返回可执行文件所在目录 = ``Path(root) / bin_sub``（``bin_sub`` 为空则返回 ``Path(root)``）。"""
        root = Path(root)
        return root / self.bin_sub if self.bin_sub else root

    def post_install(self, root):
        """解压后处理钩子，默认不做任何事。"""
        return None

    def scan_installed(self, runtime_dir):
        """扫描 ``<runtime_dir>/<工具名>/`` 下的子目录，返回已安装版本记录。

        每个子目录名即版本号；无法访问、符号链接等异常目录会被跳过。
        """
        result = []
        try:
            base = Path(runtime_dir) / self.name
            if not base.is_dir():
                return result
            entries = list(base.iterdir())
        except OSError:
            return result

        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if not entry.is_dir():
                    continue
                path = str(entry.resolve())
            except OSError:
                continue
            result.append({"ver": entry.name, "path": path, "source": "scan"})
        return result


# --------------------------------------------------------------------------- #
# Java（在线：Adoptium API）
# --------------------------------------------------------------------------- #

# 查询的 JDK feature 版本；Adoptium 的 assets/latest 接口按 feature 返回最新构建。
_JAVA_FEATURES = (8, 11, 17, 21, 23)

_JAVA_ASSETS_URL = (
    "https://api.adoptium.net/v3/assets/latest/{feature}/hotspot"
    "?architecture=x64&image_type=jdk&os=windows&vendor=eclipse"
)


class JavaProvider(Provider):
    """Java（Adoptium/Temurin）在线版本源。"""

    name = "java"
    exe_names = ["java.exe"]
    bin_sub = "bin"
    install_markers = ["bin/java.exe"]

    def mirror_urls(self, ver, official=None):
        """把官方 GitHub 直链改写为清华 TUNA 的 Adoptium 镜像（失败自动回退官方）。"""
        return _adoptium_mirrors(official or "")

    def list_versions(self, query=None):
        """从 Adoptium 获取各 LTS feature 的最新 Windows x64 JDK 压缩包下载信息。"""
        results = []
        for feature in _JAVA_FEATURES:
            raw = _http_get(_JAVA_ASSETS_URL.format(feature=feature))
            if raw is None:
                # 网络失败：返回已解析到的部分，避免整体崩溃
                break
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except (json.JSONDecodeError, ValueError):
                break
            if not isinstance(data, list):
                continue

            for asset in data:
                try:
                    binary = asset.get("binary") or {}
                    package = binary.get("package") or {}
                    link = package.get("link")
                    if not link:
                        continue
                    # 优先 Windows x64 的 .zip（排除 .tar.gz）
                    if not str(link).lower().endswith(".zip"):
                        continue

                    release = asset.get("release_name") or ""
                    ver = release[4:] if release.lower().startswith("jdk-") else release
                    if not ver:
                        ver = (asset.get("version") or {}).get("semver") or ""
                    if not ver:
                        continue

                    size = package.get("size") or 0
                    if not isinstance(size, (int, float)):
                        size = 0
                    results.append({"ver": ver, "url": link, "size": int(size)})
                except (AttributeError, TypeError, ValueError):
                    continue

        # 按 query 关键字过滤（子串匹配）
        if query:
            q = str(query)
            results = [r for r in results if q in r["ver"]]

        # 去重（同一版本只保留一次）
        seen = set()
        deduped = []
        for item in results:
            if item["ver"] in seen:
                continue
            seen.add(item["ver"])
            deduped.append(item)
        return deduped

    def build_url(self, ver):
        """在在线版本列表中查找匹配项并返回其下载 URL；找不到返回空串。"""
        items = self.list_versions(ver)
        for item in items:
            if item.get("ver") == str(ver):
                return item.get("url", "")
        return items[0].get("url", "") if items else ""

    def post_install(self, root):
        """去除解压后多余的一层目录（如 ``jdk-17.0.2+8/``）。

        若根目录下恰好存在一个包含 ``bin/java.exe`` 的子目录，
        则把该子目录内容上移到根目录并删除空子目录。
        """
        try:
            root = Path(root)
            if not root.is_dir():
                return

            candidates = []
            for child in root.iterdir():
                try:
                    if child.is_dir() and (child / "bin" / "java.exe").exists():
                        candidates.append(child)
                except OSError:
                    continue
            if len(candidates) != 1:
                return

            sub = candidates[0]
            for item in list(sub.iterdir()):
                dest = root / item.name
                try:
                    if dest.exists():
                        # 目标已存在：目录则合并内容，文件冲突则跳过
                        if dest.is_dir() and item.is_dir():
                            for inner in list(item.iterdir()):
                                inner_dest = dest / inner.name
                                if not inner_dest.exists():
                                    shutil.move(str(inner), str(inner_dest))
                    else:
                        shutil.move(str(item), str(dest))
                except OSError:
                    continue

            try:
                sub.rmdir()
            except OSError:
                pass
        except OSError:
            return


# --------------------------------------------------------------------------- #
# Node.js（在线：nodejs.org/dist/index.json）
# --------------------------------------------------------------------------- #

_NODE_INDEX_URL = "https://nodejs.org/dist/index.json"


class NodeProvider(Provider):
    """Node.js 在线版本源。"""

    name = "node"
    exe_names = ["node.exe", "npm.cmd", "npx.cmd"]
    bin_sub = ""  # Node 的 exe 就在解压根目录
    install_markers = ["node.exe"]

    def mirror_urls(self, ver, official=None):
        """Node.js 国内镜像（npmmirror / 阿里）。"""
        ver = str(ver).lstrip("v")
        fn = "node-v%s-win-x64.zip" % ver
        return [
            "https://npmmirror.com/mirrors/node/v%s/%s" % (ver, fn),
            "https://cdn.npmmirror.com/binaries/node/v%s/%s" % (ver, fn),
        ]

    def list_versions(self, query=None):
        """从 ``index.json`` 获取版本列表，去掉 ``v`` 前缀并仅保留 Windows x64 zip 版本。"""
        raw = _http_get(_NODE_INDEX_URL)
        if raw is None:
            return []
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(data, list):
            return []

        results = []
        for item in data:
            try:
                ver_raw = item.get("version") or ""
                if not ver_raw:
                    continue
                ver = ver_raw[1:] if ver_raw.startswith("v") else ver_raw
                # 仅保留包含 win-x64-zip 的版本
                if "win-x64-zip" not in (item.get("files") or []):
                    continue
                url = "https://nodejs.org/dist/v%s/node-v%s-win-x64.zip" % (ver, ver)
                results.append({"ver": ver, "url": url, "size": 0})
            except (AttributeError, TypeError):
                continue

        if query:
            q = str(query)
            results = [r for r in results if r["ver"].startswith(q)]
        return results

    def build_url(self, ver):
        """按约定 URL 规则构造 Windows x64 zip 下载地址。"""
        ver = str(ver).lstrip("v")
        return "https://nodejs.org/dist/v%s/node-v%s-win-x64.zip" % (ver, ver)

    def post_install(self, root):
        """剥离解压产生的顶层 ``node-vX-win-x64/`` 目录，把 node.exe 等文件提到版本根目录。"""
        _flatten_single_top_level(root, "node.exe")


# --------------------------------------------------------------------------- #
# 静态版本源（PHP / Maven / MySQL / Python）
# --------------------------------------------------------------------------- #


class StaticProvider(Provider):
    """静态版本表适配器。

    子类通过类属性 ``VALUES``（``[{"ver","url"}, ...]``）维护可下载版本，
    通过 ``URL_TEMPLATE``（含 ``{ver}``、``{series}`` 等占位符）支持手动输入版本号生成 URL。
    """

    VALUES = []
    URL_TEMPLATE = ""

    def list_versions(self, query=None):
        """从静态版本表按 ``query`` 前缀过滤返回。"""
        items = [dict(v) for v in self.VALUES]
        if query:
            q = str(query)
            items = [i for i in items if str(i.get("ver", "")).startswith(q)]
        return items

    def build_url(self, ver):
        """先查静态表；未命中则用 URL 模板替换版本号生成；均无则返回空串。"""
        ver = str(ver)
        for item in self.VALUES:
            if str(item.get("ver")) == ver:
                return item.get("url", "")
        if not self.URL_TEMPLATE:
            return ""

        parts = ver.split(".")
        ctx = {
            "ver": ver,
            "major": parts[0] if parts else ver,
            "minor": parts[1] if len(parts) > 1 else "",
            "series": ".".join(parts[:2]) if len(parts) >= 2 else ver,
        }
        try:
            return self.URL_TEMPLATE.format(**ctx)
        except (KeyError, IndexError, ValueError):
            return ""


# --------------------------- PHP --------------------------- #


class PhpProvider(StaticProvider):
    """PHP（Windows NTS 版）静态版本源。"""

    name = "php"
    exe_names = ["php.exe"]
    bin_sub = ""  # php.exe 就在解压根目录
    install_markers = ["php.exe"]

    VALUES = [
        {
            "ver": "8.3.6",
            "url": "https://windows.php.net/downloads/releases/php-8.3.6-nts-Win32-vs16-x64.zip",
        },
        {
            "ver": "8.2.18",
            "url": "https://windows.php.net/downloads/releases/php-8.2.18-nts-Win32-vs16-x64.zip",
        },
        {
            "ver": "8.1.28",
            "url": "https://windows.php.net/downloads/releases/php-8.1.28-nts-Win32-vs16-x64.zip",
        },
    ]
    URL_TEMPLATE = (
        "https://windows.php.net/downloads/releases/php-{ver}-nts-Win32-vs16-x64.zip"
    )

    def post_install(self, root):
        """由 ``php.ini-development`` 生成 ``php.ini`` 并设置扩展目录/zip 扩展/时区。"""
        try:
            root = Path(root)
            ini_dev = root / "php.ini-development"
            ini = root / "php.ini"
            if not ini_dev.exists():
                return
            try:
                text = ini_dev.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return

            # extension_dir 使用绝对路径（正斜杠，避免 ini 中反斜杠被转义）
            ext_dir = str(root).replace("\\", "/") + "/ext"
            text = text.replace(
                ';extension_dir = "ext"', 'extension_dir = "%s"' % ext_dir
            )
            text = text.replace(
                ";extension_dir = \"ext\"", 'extension_dir = "%s"' % ext_dir
            )

            # 取消 zip 扩展的注释
            text = text.replace(";extension=zip", "extension=zip")
            text = text.replace(";extension = zip", "extension=zip")

            # 若无生效的 date.timezone 设置则追加
            has_timezone = any(
                line.strip().startswith("date.timezone")
                for line in text.splitlines()
            )
            if not has_timezone:
                if not text.endswith("\n"):
                    text += "\n"
                text += 'date.timezone = "Asia/Shanghai"\n'

            try:
                ini.write_text(text, encoding="utf-8")
            except OSError:
                return
        except OSError:
            return


# --------------------------- Maven --------------------------- #


_MVN_SETTINGS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">
  <mirrors>
    <mirror>
      <id>aliyun</id>
      <mirrorOf>central</mirrorOf>
      <name>aliyun public</name>
      <url>https://maven.aliyun.com/repository/public</url>
    </mirror>
  </mirrors>
</settings>
"""


class MavenProvider(StaticProvider):
    """Maven（Apache 官方归档）静态版本源。"""

    name = "maven"
    exe_names = ["mvn.cmd"]
    bin_sub = "bin"
    install_markers = ["bin/mvn.cmd"]

    def mirror_urls(self, ver, official=None):
        """Maven 国内镜像（清华 / 阿里）；旧版本镜像可能缺失，自动回退官方归档。"""
        ver = str(ver)
        fn = "apache-maven-%s-bin.zip" % ver
        return [
            "https://mirrors.tuna.tsinghua.edu.cn/apache/maven/maven-3/%s/binaries/%s" % (ver, fn),
            "https://mirrors.aliyun.com/apache/maven/maven-3/%s/binaries/%s" % (ver, fn),
        ]

    VALUES = [
        {
            "ver": "3.9.6",
            "url": "https://archive.apache.org/dist/maven/maven-3/3.9.6/binaries/apache-maven-3.9.6-bin.zip",
        },
        {
            "ver": "3.9.9",
            "url": "https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.zip",
        },
        {
            "ver": "3.8.8",
            "url": "https://archive.apache.org/dist/maven/maven-3/3.8.8/binaries/apache-maven-3.8.8-bin.zip",
        },
    ]
    URL_TEMPLATE = (
        "https://archive.apache.org/dist/maven/maven-3/{ver}/binaries/apache-maven-{ver}-bin.zip"
    )

    def post_install(self, root):
        """先剥离解压产生的顶层 ``apache-maven-x.y.z/`` 目录，再在
        ``<root>/conf/`` 下生成含阿里云镜像的 ``settings.xml``（已存在则不覆盖）。
        """
        # 1) 剥离多余顶层目录（保证 bin/、conf/ 直接位于版本根目录下）
        try:
            root = Path(root)
            if root.is_dir():
                candidates = [
                    c for c in root.iterdir()
                    if c.is_dir() and (c / "bin" / "mvn.cmd").exists()
                ]
                if len(candidates) == 1:
                    sub = candidates[0]
                    for item in list(sub.iterdir()):
                        dest = root / item.name
                        try:
                            if dest.exists() and dest.is_dir() and item.is_dir():
                                for inner in item.iterdir():
                                    inner_dest = dest / inner.name
                                    if not inner_dest.exists():
                                        shutil.move(str(inner), str(inner_dest))
                            elif not dest.exists():
                                shutil.move(str(item), str(dest))
                        except OSError:
                            continue
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
        except OSError:
            pass

        # 2) 生成 settings.xml
        try:
            root = Path(root)
            conf_dir = root / "conf"
            settings = conf_dir / "settings.xml"
            if settings.exists():
                return
            try:
                conf_dir.mkdir(parents=True, exist_ok=True)
                settings.write_text(_MVN_SETTINGS_XML, encoding="utf-8")
            except OSError:
                return
        except OSError:
            return


# --------------------------- Python --------------------------- #


class PythonProvider(StaticProvider):
    """Python（Windows embed 版）静态版本源。"""

    name = "python"
    exe_names = ["python.exe", "pip.exe"]
    bin_sub = ""  # embed zip 的 python.exe 就在解压根目录
    install_markers = ["python.exe"]

    def mirror_urls(self, ver, official=None):
        """Python 国内镜像（华为云 / npmmirror binary）。"""
        ver = str(ver)
        fn = "python-%s-embed-amd64.zip" % ver
        return [
            "https://mirrors.huaweicloud.com/python/%s/%s" % (ver, fn),
            "https://registry.npmmirror.com/-/binary/python/%s/%s" % (ver, fn),
        ]

    VALUES = [
        {
            "ver": "3.12.4",
            "url": "https://www.python.org/ftp/python/3.12.4/python-3.12.4-embed-amd64.zip",
        },
        {
            "ver": "3.11.9",
            "url": "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip",
        },
        {
            "ver": "3.10.11",
            "url": "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip",
        },
    ]
    URL_TEMPLATE = "https://www.python.org/ftp/python/{ver}/python-{ver}-embed-amd64.zip"

    # 需要取消注释的 _pth 行内容
    _UNCOMMENT = ("Lib", "Lib\\site-packages", "Lib/site-packages")

    def post_install(self, root):
        """编辑 ``python*._pth``：取消 Lib 行注释并确保存在 ``importsite``。"""
        try:
            root = Path(root)
            for pth_path in glob.glob(str(root / "python*._pth")):
                try:
                    text = Path(pth_path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                out_lines = []
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        body = stripped[1:].strip()
                        if body in self._UNCOMMENT:
                            out_lines.append(body)
                            continue
                    out_lines.append(line)

                if not any(l.strip() == "importsite" for l in out_lines):
                    out_lines.append("importsite")

                try:
                    Path(pth_path).write_text(
                        "\n".join(out_lines) + "\n", encoding="utf-8"
                    )
                except OSError:
                    continue
        except OSError:
            return


# --------------------------- MySQL --------------------------- #


class MysqlProvider(StaticProvider):
    """MySQL（社区版 winx64 zip）静态版本源。"""

    name = "mysql"
    exe_names = ["mysql.exe", "mysqld.exe"]
    bin_sub = "bin"
    install_markers = ["bin/mysqld.exe"]

    def mirror_urls(self, ver, official=None):
        # MySQL 官方 winx64 zip 无可靠国内镜像（清华/中科大/华为/腾讯均无该路径，
        # 实测均 404），改用 Oracle 官方 CDN cdn.mysql.com（对非浏览器 UA 亦放行）。
        return []

    VALUES = [
        {
            "ver": "8.0.37",
            "url": "https://cdn.mysql.com/Downloads/MySQL-8.0/mysql-8.0.37-winx64.zip",
        },
        {
            "ver": "8.4.0",
            "url": "https://cdn.mysql.com/Downloads/MySQL-8.4/mysql-8.4.0-winx64.zip",
        },
    ]
    URL_TEMPLATE = (
        "https://cdn.mysql.com/Downloads/MySQL-{series}/mysql-{ver}-winx64.zip"
    )

    def post_install(self, root):
        """剥离解压产生的顶层 ``mysql-x.y.z-winx64/`` 目录，再生成最小
        ``my.ini`` 骨架（已存在则跳过）。完整配置逻辑见 Task 7。"""
        _flatten_single_top_level(root, "bin/mysqld.exe")
        try:
            root = Path(root)
            if not root.is_dir():
                return
            ini = root / "my.ini"
            if ini.exists():
                return
            content = (
                "[mysqld]\n"
                "# DevBox 生成的占位配置，完整实例配置在服务管理流程中写入\n"
                "basedir=%s\n" % str(root).replace("\\", "/")
            )
            try:
                # GBK 写入：与 mysqld 按 ANSI 代码页读取选项文件一致，避免中文路径乱码
                ini.write_text(content, encoding="gbk")
            except OSError:
                return
        except OSError:
            return


# --------------------------- Redis --------------------------- #


class RedisProvider(StaticProvider):
    """Redis（Windows 版 zip）静态版本源。

    Windows 版 Redis（tporadowski / MicrosoftArchive 发行版）解压后 ``redis-server.exe``
    等可执行文件直接位于压缩包根目录（无 bin 子目录），并自带
    ``redis.windows.conf`` / ``redis.windows-service.conf``。服务化与多实例配置
    逻辑见 :mod:`app.redissvc`。
    """

    name = "redis"
    exe_names = ["redis-server.exe", "redis-cli.exe", "redis-benchmark.exe"]
    bin_sub = ""  # Windows 版 exe 就在解压根目录
    install_markers = ["redis-server.exe"]

    def mirror_urls(self, ver, official=None):
        """GitHub 加速镜像（gh-proxy 前缀）；失败自动回退官方直链。"""
        if not official:
            return []
        return [
            "https://mirror.ghproxy.com/" + official,
            "https://ghproxy.com/" + official,
        ]

    VALUES = [
        {
            "ver": "5.0.14.1",
            "url": "https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip",
        },
        {
            "ver": "4.0.14.3",
            "url": "https://github.com/tporadowski/redis/releases/download/v4.0.14.3/Redis-x64-4.0.14.3.zip",
        },
        {
            "ver": "3.2.100",
            "url": "https://github.com/MicrosoftArchive/redis/releases/download/win-3.2.100/Redis-x64-3.2.100.zip",
        },
    ]
    URL_TEMPLATE = (
        "https://github.com/tporadowski/redis/releases/download/v{ver}/Redis-x64-{ver}.zip"
    )

    def post_install(self, root):
        """剥离解压可能产生的单层顶层目录（保证 exe 直接位于版本根目录）。"""
        _flatten_single_top_level(root, "redis-server.exe")


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

def _flatten_single_top_level(root, marker_rel_path):
    """若 root 下恰有一个包含 ``marker_rel_path`` 的子目录，则把其内容上移到 root。

    用于剥离 zip 解压时常见的单层顶层目录（如 ``apache-maven-3.9.9/``、
    ``node-v22-win-x64/``、``mysql-8.0.37-winx64/``），与 Java 的行为一致。
    """
    try:
        root = Path(root)
        if not root.is_dir():
            return
        candidates = []
        for child in root.iterdir():
            try:
                if child.is_dir() and (child / marker_rel_path).exists():
                    candidates.append(child)
            except OSError:
                continue
        if len(candidates) != 1:
            return

        sub = candidates[0]
        for item in list(sub.iterdir()):
            dest = root / item.name
            try:
                if dest.exists():
                    # 目标已存在：目录则合并内容，文件冲突则跳过
                    if dest.is_dir() and item.is_dir():
                        for inner in list(item.iterdir()):
                            inner_dest = dest / inner.name
                            if not inner_dest.exists():
                                shutil.move(str(inner), str(inner_dest))
                else:
                    shutil.move(str(item), str(dest))
            except OSError:
                continue

        try:
            sub.rmdir()
        except OSError:
            pass
    except OSError:
        return


PROVIDERS = {
    "java": JavaProvider(),
    "node": NodeProvider(),
    "python": PythonProvider(),
    "php": PhpProvider(),
    "maven": MavenProvider(),
    "mysql": MysqlProvider(),
    "redis": RedisProvider(),
}

TOOL_ORDER = ["java", "node", "python", "php", "maven", "mysql", "redis"]
