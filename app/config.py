"""DevBox 配置层。

负责 box.json 的加载、默认值合并、目录推导与原子保存，
以及 current / installed 版本记录的读写。仅依赖 Python 标准库。
"""

import copy
import json
import os
from pathlib import Path

# DevBox 数据根目录：所有下载资源、解压版本、配置文件统一存放在 D 盘，
# 严禁写入 C 盘（严格控制 C 盘占用）。若 D 盘不可用则回退到程序所在目录的 data 子目录。
ROOT_DIR = "D:\\环境管理\\data"


def _app_dir():
    """返回程序（或 exe）所在目录。

    打包（PyInstaller）环境下基于 sys.executable 定位 exe 所在目录，
    源码运行时基于 app 包父目录。
    """
    import sys
    if getattr(sys, "frozen", False):
        # PyInstaller 冻结环境：exe 所在目录
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _default_data_root():
    """返回可用的数据根目录：优先 ``D:\\环境管理\\data``。

    判断标准：D 盘根目录存在且可写（用 mkdir 探测）。
    不可用时回退到程序所在目录下的 ``data``（仍在非系统盘），并打印告警。
    """
    preferred = Path(ROOT_DIR)
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError as exc:
        fallback = _app_dir() / "data"
        print(
            "[DevBox] 警告：默认数据目录 %s 不可用(%s)，"
            "已回退到 %s" % (preferred, exc, fallback)
        )
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return fallback


# 默认配置模板。新增字段会自动合并补齐，避免 KeyError。
DEFAULT = {
    "runtime_dir": "",   # 运行时为推导出的绝对路径字符串
    "cache_dir": "",
    "shim_dir": "",
    "mirror": "cn",         # 下载源："cn"=国内镜像优先（自动回退官方），"default"=官方源
    "current": {},       # {tool: ver}
    "installed": {},     # {tool: [{"ver", "path", "source"}, ...]}
    "theme": "",         # 界面主题（light/dark），空串表示默认浅色
}


class Box:
    """DevBox 配置对象。

    - self.path: box.json 的 Path
    - self.data: 合并默认值后的配置字典
    """

    def __init__(self, path=None):
        # 解析配置文件路径：默认位于 D:\环境管理\data\box.json（D 盘不可用时回退）
        if path is None:
            self.path = _default_data_root() / "box.json"
        else:
            self.path = Path(path)

        # 加载配置：文件不存在或解析失败均回退到默认配置，不崩溃
        loaded = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    loaded = {}
            except json.JSONDecodeError as exc:
                print("[DevBox] 警告：配置文件解析失败，已使用默认配置。(%s)" % exc)
                loaded = {}
            except OSError as exc:
                print("[DevBox] 警告：读取配置文件失败，已使用默认配置。(%s)" % exc)
                loaded = {}

        # 深拷贝默认值再合并，保证字段始终存在。
        # 必须深拷贝：DEFAULT 中的 current/installed 是嵌套 dict，
        # 若用 {**DEFAULT, **loaded} 浅合并，多个 Box 实例会共享同一嵌套对象，
        # 任一实例的 add_installed 会污染其他实例（及 DEFAULT 模板）。
        self.data = copy.deepcopy(DEFAULT)
        self.data.update(loaded)
        # current / installed 必须是 dict，防止被损坏的配置污染
        if not isinstance(self.data.get("current"), dict):
            self.data["current"] = {}
        if not isinstance(self.data.get("installed"), dict):
            self.data["installed"] = {}

        # 推导默认目录：以 box.json 所在目录（DevBox 根目录）为基准
        root = self.path.parent
        if not self.data.get("runtime_dir"):
            self.data["runtime_dir"] = str(root / "runtimes")
        if not self.data.get("cache_dir"):
            self.data["cache_dir"] = str(root / "cache")
        if not self.data.get("shim_dir"):
            self.data["shim_dir"] = str(root / "shim")

        # 确保所有目录存在（任一失败仅告警，不崩溃）
        for d in (
            self.path.parent,
            self.data["runtime_dir"],
            self.data["cache_dir"],
            self.data["shim_dir"],
            str(root / "logs"),
        ):
            try:
                Path(d).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print("[DevBox] 警告：创建目录失败 %s (%s)" % (d, exc))

    def save(self):
        """原子写入配置：先写临时文件，再用 os.replace 覆盖 box.json。"""
        tmp = self.path.parent / (self.path.name + ".tmp")
        text = json.dumps(self.data, ensure_ascii=False, indent=2)
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(self.path))

    def current(self, tool):
        """返回指定工具当前激活的版本号，未设置返回 None。"""
        return self.data["current"].get(tool)

    def set_current(self, tool, ver):
        """设置指定工具的当前版本并保存。"""
        self.data["current"][tool] = ver
        self.save()

    def add_installed(self, tool, ver, path, source):
        """添加已安装版本记录；同版本已存在时更新其 path/source（去重）。"""
        items = self.data["installed"].setdefault(tool, [])
        record = {"ver": ver, "path": str(path), "source": source}
        for item in items:
            if item.get("ver") == ver:
                item.update(record)
                break
        else:
            items.append(record)
        self.save()

    def remove_installed(self, tool, ver):
        """移除指定工具的某个版本记录并保存。"""
        items = self.data["installed"].get(tool, [])
        self.data["installed"][tool] = [
            i for i in items if i.get("ver") != ver
        ]
        self.save()

    def find_installed(self, tool, ver):
        """返回指定工具/版本的记录 dict，未找到返回 None。"""
        for item in self.data["installed"].get(tool, []):
            if item.get("ver") == ver:
                return item
        return None

    def installed_list(self, tool):
        """返回指定工具的已安装版本记录列表，无则返回空列表。"""
        return self.data["installed"].get(tool, [])
