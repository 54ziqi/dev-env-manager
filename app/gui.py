"""DevBox 图形界面层。

使用 tkinter/ttk 实现左右分栏界面：左侧为工具列表，右侧为
「已安装版本 / 下载新版本 / 设置 / MySQL 服务」四个标签页，串联配置层、版本源
适配器、Shim 生成、环境变量同步、下载解压与 MySQL 服务端实例管理各模块。

线程安全约定：
- 所有控件操作、文件对话框与消息弹窗仅在主线程（Tk 事件循环）中调用；
- 耗时的下载与版本列表网络请求放入守护子线程；
- 子线程只通过 ``root.after(0, ...)``（封装为 ``_post``）把回调转投到主线程。

仅依赖 Python 标准库。
"""

import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk
from urllib.parse import urlparse

from app import mysqlsvc
from app import redissvc
from app import systemscan
from app import uninstall
from app.config import Box
from app.downloader import download_any, unpack, validate_zip_package
from app.env import sync_homes
from app.providers import PROVIDERS, TOOL_ORDER
from app.shim import write_shims

# 工具英文名 -> 中文显示名。
TOOL_NAMES = {
    "java": "Java",
    "node": "Node.js",
    "python": "Python",
    "php": "PHP",
    "maven": "Maven",
    "mysql": "MySQL",
    "redis": "Redis",
}

# 首次运行提示通过 box 上的临时属性传递，GUI 启动后由主线程延迟弹出。
NOTICE_ATTR = "_devbox_startup_notice"

# ------------------------------------------------------------------ #
# 主题与配色（仅用于样式/展示，不参与任何业务逻辑）
# ------------------------------------------------------------------ #
#
# 每个主题是一组「同名设计令牌」的字典。界面所有颜色都取自当前主题令牌，
# 切换主题即用对应令牌重新应用 ttk 样式并对原生控件重新着色，无需重建窗口。
#
# 令牌含义：
#   primary/primary_hover/primary_press  主色及交互态
#   primary_light                        主色浅底（选中/焦点态）
#   on_primary                           主色之上的文字色（主按钮/危险按钮）
#   page / card                          页面背景 / 卡片控件背景
#   border / border_input / border_hover 边框及交互态
#   text / text_sub / text_muted         正文 / 次要 / 占位禁用
#   head_bg / head_hover                 表头·状态栏·槽背景及其交互态
#   btn_hover / btn_press                次要按钮交互态
#   success / success_bg                 成功语义色
#   danger / danger_hover / danger_press 危险语义色
#   warn_bg / warn_text                  警告浅底与文字
#   zebra                                表格斑马纹
#   thumb / thumb_hover / thumb_press    滚动条滑块及交互态

THEMES = {
    "light": {
        "primary": "#3370ff",
        "primary_hover": "#2861e0",
        "primary_press": "#1f4fd8",
        "primary_light": "#e8f0ff",
        "on_primary": "#ffffff",
        "page": "#f5f6f8",
        "card": "#ffffff",
        "border": "#e5e7eb",
        "border_input": "#d0d5dd",
        "border_hover": "#b9bec6",
        "text": "#1f2329",
        "text_sub": "#646a73",
        "text_muted": "#8f959e",
        "head_bg": "#f2f3f5",
        "head_hover": "#e7eaef",
        "btn_hover": "#f2f4f7",
        "btn_press": "#e9ecf0",
        "success": "#2ea44f",
        "success_bg": "#eaf6ee",
        "danger": "#d83931",
        "danger_hover": "#c9322b",
        "danger_press": "#b52b24",
        "warn_bg": "#fdf5e6",
        "warn_text": "#a05000",
        "zebra": "#fafbfc",
        "thumb": "#c9cdd4",
        "thumb_hover": "#aab2bd",
        "thumb_press": "#8d97a3",
    },
    "dark": {
        "primary": "#4c8dff",
        "primary_hover": "#639bff",
        "primary_press": "#3b7bff",
        "primary_light": "#22314f",
        "on_primary": "#ffffff",
        "page": "#14161a",
        "card": "#1d2025",
        "border": "#2c313a",
        "border_input": "#3a4049",
        "border_hover": "#454c56",
        "text": "#e8eaed",
        "text_sub": "#a7adb7",
        "text_muted": "#7a828d",
        "head_bg": "#232830",
        "head_hover": "#2b313a",
        "btn_hover": "#262b32",
        "btn_press": "#2d333b",
        "success": "#4cc76a",
        "success_bg": "#17301f",
        "danger": "#f2554d",
        "danger_hover": "#ff6b63",
        "danger_press": "#e0433b",
        "warn_bg": "#33291a",
        "warn_text": "#e0a94a",
        "zebra": "#22262c",
        "thumb": "#4a515b",
        "thumb_hover": "#5a626d",
        "thumb_press": "#6b7480",
    },
}

# 主题 key -> 界面下拉展示名
THEME_LABELS = {"light": "浅色", "dark": "深色"}
DEFAULT_THEME = "light"

# 应用元信息（用于「关于」对话框与许可/版权展示）
APP_NAME = "开发环境管理器 (DevBox)"
APP_VERSION = "1.0.0"
APP_AUTHOR = "子柒"
APP_LICENSE = "PolyForm Noncommercial License 1.0.0"
APP_COPYRIGHT = "© 2026 子柒"

# 中文字体候选（依次回退）
FONT_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")

# 「来源」列原始值 -> 中文显示名（仅改变展示，不改 box.json 存储值）
SOURCE_NAMES = {
    "download": "下载",
    "import": "导入",
    "scan": "扫描",
    "system": "系统",
}


def _fmt_mb(num_bytes):
    """把字节数格式化为 MB 文本，非法值返回 "-"。"""
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "0 MB"
    return "%.1f MB" % (n / (1024.0 * 1024.0))


def _java_major(ver):
    """从 Java 版本号中提取主版本：``1.8.0`` -> 8，``17.0.2`` -> 17；解析失败返回 None。"""
    if not ver:
        return None
    v = str(ver)
    if v.startswith("1."):
        parts = v.split(".")
        if len(parts) > 1 and parts[1].isdigit():
            return int(parts[1])
        return None
    head = v.split(".")[0]
    return int(head) if head.isdigit() else None


def _guess_version_from_name(path):
    """从压缩包文件名推断默认版本号（取文件名主体）。"""
    return Path(path).stem or "1.0"


def _installed_has_marker(target_dir, marker):
    """判断解压后的目录是否含指定关键文件（相对路径后缀，忽略大小写）。

    先查直接拼接路径；未命中则按末级文件名递归搜索，匹配以 marker 结尾的路径
    （兼容压缩包带顶层目录、post_install 展平与否等不同结构）。
    """
    marker_norm = str(marker).replace("\\", "/").lower().lstrip("/")
    direct = Path(target_dir) / marker
    if direct.is_file():
        return True
    base = marker_norm.rsplit("/", 1)[-1]
    try:
        for cand in Path(target_dir).rglob(base):
            if cand.is_file() and str(cand).replace("\\", "/").lower().endswith(marker_norm):
                return True
    except OSError:
        return False
    return False


def _pick_font_family(root):
    """挑选可用的中文界面字体：雅黑优先，缺失时依次回退。"""
    try:
        families = {str(name).lower() for name in tkfont.families(root)}
    except Exception:  # noqa: BLE001 - 字体探测失败直接用首选名
        return FONT_CANDIDATES[0]
    for name in FONT_CANDIDATES:
        if name.lower() in families:
            return name
    return FONT_CANDIDATES[0]


def _font(family, size, bold=False):
    """构造 Tk 字体元组。"""
    return (family, size, "bold") if bold else (family, size)


def _source_label(source):
    """把「来源」原始值映射为中文显示名；未知值原样显示。"""
    text = str(source or "")
    return SOURCE_NAMES.get(text, text or "-")


def _center_on_parent(dialog, parent):
    """把对话框按自然尺寸居中到父窗口（保持比例、不强制拉伸）。"""
    dialog.update_idletasks()
    try:
        w = dialog.winfo_reqwidth()
        h = dialog.winfo_reqheight()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
    except tk.TclError:
        return
    x = px + max(0, (pw - w) // 2)
    y = py + max(0, (ph - h) // 3)
    dialog.geometry("+%d+%d" % (x, y))


class _FormDialog(tk.Toplevel):
    """密码/表单输入对话框：密码可见切换、统一尺寸、回车确认。

    取代多个 ``simpledialog.askstring`` 串联，减少弹窗、改善比例与可读性。
    ``fields`` 为 ``(key, label, kind[, default])`` 列表，``kind`` 取
    ``"text"`` 或 ``"password"``（密码框默认掩码，带“显示”勾选切换）。
    构造即阻塞（模态），确认后 ``result`` 为 ``{key: value}``，取消为 ``None``。
    """

    def __init__(self, app, title, fields, ok_text="确定"):
        super().__init__(app.root)
        self.app = app
        self.result = None
        self._vars = {}
        self._entries = {}
        self._show_vars = {}

        C = app.palette
        fam = app.font_family
        self.title(title)
        self.resizable(False, False)
        self.transient(app.root)
        self.configure(bg=C["card"])

        body = ttk.Frame(self, padding=(18, 16, 18, 8))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        for i, spec in enumerate(fields):
            key, label, kind = spec[0], spec[1], spec[2]
            default = spec[3] if len(spec) > 3 else ""
            ttk.Label(body, text=label, anchor="e").grid(
                row=i, column=0, sticky="e", padx=(0, 10), pady=6
            )
            var = tk.StringVar(value=str(default))
            self._vars[key] = var
            entry = ttk.Entry(body, textvariable=var, width=30, font=_font(fam, 10))
            entry.grid(row=i, column=1, sticky="ew", pady=6)
            self._entries[key] = entry
            if kind == "password":
                entry.config(show="*")
                shown = tk.BooleanVar(value=False)
                self._show_vars[key] = shown
                ttk.Checkbutton(
                    body, text="显示", variable=shown,
                    command=lambda k=key, s=shown: self._toggle(k, s),
                ).grid(row=i, column=2, padx=(10, 0))
            else:
                ttk.Label(body, text=" ").grid(row=i, column=2)

        btns = ttk.Frame(self, padding=(18, 4, 18, 16))
        btns.pack(fill="x")
        ttk.Button(btns, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(
            btns, text=ok_text, style="Primary.TButton", command=self._ok
        ).pack(side="right", padx=(0, 8))

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        if self._entries:
            next(iter(self._entries.values())).focus_set()
        _center_on_parent(self, app.root)
        self.grab_set()
        app.root.wait_window(self)

    def _toggle(self, key, shown):
        """切换指定密码框的明文/掩码显示。"""
        self._entries[key].config(show="" if shown.get() else "*")

    def _ok(self):
        self.result = {k: v.get() for k, v in self._vars.items()}
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class _TempPasswordDialog(tk.Toplevel):
    """展示初始化生成的临时密码：默认明文可选中、可隐藏、一键复制到剪贴板。"""

    def __init__(self, app, password):
        super().__init__(app.root)
        self._pwd = str(password or "")
        C = app.palette
        fam = app.font_family
        self.title("临时密码")
        self.resizable(False, False)
        self.transient(app.root)
        self.configure(bg=C["card"])

        body = ttk.Frame(self, padding=(18, 16))
        body.pack(fill="both", expand=True)
        ttk.Label(
            body, text="root@localhost 临时密码（首次登录后请修改）："
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        var = tk.StringVar(value=self._pwd)
        # 必须把 StringVar/BooleanVar 挂到 self 上保持引用：否则 __init__ 返回后局部变量
        # 被 GC，tkinter.Variable.__del__ 会 unset 底层 Tcl 变量，导致输入框变空。
        self._var = var
        entry = ttk.Entry(body, textvariable=self._var, width=30, font=_font(fam, 11))
        self._entry = entry
        entry.grid(row=1, column=0, sticky="ew")
        self._shown = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            body, text="显示", variable=self._shown,
            command=lambda: self._toggle(entry, self._shown),
        ).grid(row=1, column=1, padx=(10, 0))
        ttk.Button(
            body, text="复制", style="Primary.TButton", command=self._copy
        ).grid(row=1, column=2, padx=(10, 0))
        self._status = ttk.Label(body, text="", font=_font(fam, 9))
        self._status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _center_on_parent(self, app.root)
        self.grab_set()

    def _toggle(self, entry, shown):
        entry.config(show="" if shown.get() else "*")

    def _copy(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(self._pwd)
            self._status.config(text="已复制到剪贴板")
        except tk.TclError:
            pass


class App:
    """DevBox 主界面。

    实例属性：
    - ``root``：顶层 ``tk.Tk``；
    - ``box``：配置对象；
    - ``tool``：当前选中的工具英文名。
    """

    def __init__(self, box):
        self.box = box
        self.tool = TOOL_ORDER[0]
        self._downloading = False   # 是否有下载正在进行（切换工具时保留进度显示）

        self.root = tk.Tk()
        self.root.title("环境管理 - 多版本开发环境管理器")
        self.root.minsize(600, 400)
        self._set_window_icon()

        # 当前主题（从配置读取，非法值回退默认）与激活配色令牌
        saved = str(self.box.data.get("theme") or "").strip().lower()
        self.theme_name = saved if saved in THEMES else DEFAULT_THEME
        self.palette = dict(THEMES[self.theme_name])
        # 需要在主题切换时重新着色的原生（非 ttk）控件
        self._border_lines = []  # 1px 分隔线，取 border 色
        self._accent_bars = []   # 主色强调条，取 primary 色

        # 界面字体与统一样式（纯装饰，配置失败不影响功能）
        self.font_family = _pick_font_family(self.root)
        self._apply_styles()

        self._build_ui()
        self._center_window(1060, 680)
        # 默认选中第一个工具，触发三个标签页内容刷新
        self._select_tool(0)
        # 初始高亮左侧列表中的当前工具（纯展示，不触发选中事件）
        self.tool_listbox.selection_set(0)

    # ------------------------------------------------------------------ #
    # 样式与窗口几何（装饰性配置，全部优雅降级）
    # ------------------------------------------------------------------ #

    def _apply_styles(self):
        """集中配置 ttk 样式（clam 主题）。

        配色/字体遵循统一设计规范；整段配置用 try/except 包裹，
        任何一步失败都只告警忽略，绝不影响界面启动与业务功能。
        """
        try:
            style = ttk.Style(self.root)
            style.theme_use("clam")
            C = self.palette

            fam = self.font_family
            f_body = _font(fam, 10)        # 正文 10pt
            f_small = _font(fam, 9)        # 次要 9pt
            f_card = _font(fam, 11, True)  # 卡片/标签标题 11pt 加粗
            f_btn = _font(fam, 10)
            f_head = _font(fam, 10, True)  # 表头加粗

            # ---------- 页面与容器 ----------
            style.configure("TFrame", background=C["page"])
            style.configure("Header.TFrame", background=C["card"])
            style.configure("Status.TFrame", background=C["head_bg"])
            style.configure(
                "TLabelframe",
                background=C["card"],
                bordercolor=C["border"],
                borderwidth=1,
                relief="solid",
            )
            style.configure(
                "TLabelframe.Label",
                background=C["card"],
                foreground=C["text"],
                font=f_card,
            )

            # ---------- 文字标签 ----------
            style.configure(
                "TLabel",
                background=C["page"],
                foreground=C["text"],
                font=f_body,
            )
            style.configure(
                "Title.TLabel",
                background=C["card"],
                foreground=C["text"],
                font=_font(fam, 16, True),
            )
            style.configure(
                "Subtitle.TLabel",
                background=C["card"],
                foreground=C["text_sub"],
                font=f_small,
            )
            style.configure(
                "Hint.TLabel",
                background=C["page"],
                foreground=C["text_muted"],
                font=f_small,
            )
            style.configure(
                "Sub.TLabel",
                background=C["page"],
                foreground=C["text_sub"],
                font=f_small,
            )
            style.configure(
                "Status.TLabel",
                background=C["head_bg"],
                foreground=C["text_sub"],
                font=f_small,
                padding=(10, 6),
            )
            style.configure(
                "Warn.TLabel",
                background=C["warn_bg"],
                foreground=C["warn_text"],
                font=f_small,
                padding=(10, 6),
            )

            # ---------- 按钮（默认 / 主操作 / 危险） ----------
            style.configure(
                "TButton",
                background=C["card"],
                foreground=C["text"],
                bordercolor=C["border_input"],
                lightcolor=C["card"],
                darkcolor=C["card"],
                focusthickness=1,
                focuscolor=C["primary_light"],
                padding=(12, 6),
                font=f_btn,
            )
            style.map(
                "TButton",
                background=[
                    ("pressed", C["btn_press"]),
                    ("active", C["btn_hover"]),
                    ("disabled", C["card"]),
                ],
                bordercolor=[("active", C["border_hover"]), ("pressed", C["border_hover"])],
                foreground=[("disabled", C["text_muted"])],
            )
            style.configure(
                "Primary.TButton",
                background=C["primary"],
                foreground=C["on_primary"],
                bordercolor=C["primary"],
                lightcolor=C["primary"],
                darkcolor=C["primary"],
                focusthickness=1,
                focuscolor=C["primary_light"],
                padding=(12, 6),
                font=f_btn,
            )
            style.map(
                "Primary.TButton",
                background=[
                    ("pressed", C["primary_press"]),
                    ("active", C["primary_hover"]),
                    ("disabled", C["primary_hover"]),
                ],
                bordercolor=[
                    ("pressed", C["primary_press"]),
                    ("active", C["primary_hover"]),
                ],
                foreground=[("disabled", C["on_primary"])],
            )
            style.configure(
                "Danger.TButton",
                background=C["danger"],
                foreground=C["on_primary"],
                bordercolor=C["danger"],
                lightcolor=C["danger"],
                darkcolor=C["danger"],
                focusthickness=1,
                focuscolor=C["primary_light"],
                padding=(12, 6),
                font=f_btn,
            )
            style.map(
                "Danger.TButton",
                background=[
                    ("pressed", C["danger_press"]),
                    ("active", C["danger_hover"]),
                    ("disabled", C["danger_hover"]),
                ],
                bordercolor=[
                    ("pressed", C["danger_press"]),
                    ("active", C["danger_hover"]),
                ],
                foreground=[("disabled", C["on_primary"])],
            )

            # ---------- 表格 ----------
            style.configure(
                "Treeview",
                background=C["card"],
                fieldbackground=C["card"],
                foreground=C["text"],
                bordercolor=C["border"],
                relief="flat",
                font=f_body,
                rowheight=34,
            )
            style.map(
                "Treeview",
                background=[("selected", C["primary_light"])],
                foreground=[("selected", C["text"])],
            )
            style.configure(
                "Treeview.Heading",
                background=C["head_bg"],
                foreground=C["text"],
                font=f_head,
                relief="flat",
                borderwidth=1,
                bordercolor=C["border"],
            )
            style.map(
                "Treeview.Heading", background=[("active", C["head_hover"])]
            )

            # ---------- 标签页 ----------
            style.configure(
                "TNotebook",
                background=C["page"],
                borderwidth=0,
                tabmargins=(6, 6, 0, 0),
            )
            style.configure(
                "TNotebook.Tab",
                background=C["head_bg"],
                foreground=C["text_sub"],
                padding=(14, 7),
                font=f_body,
                borderwidth=0,
            )
            style.map(
                "TNotebook.Tab",
                background=[("selected", C["card"]), ("active", C["head_hover"])],
                foreground=[("selected", C["primary"])],
                font=[("selected", _font(fam, 10, True))],
            )

            # ---------- 输入框 / 下拉框 ----------
            style.configure(
                "TEntry",
                fieldbackground=C["card"],
                background=C["card"],
                bordercolor=C["border_input"],
                lightcolor=C["card"],
                darkcolor=C["card"],
                foreground=C["text"],
                insertcolor=C["text"],
                padding=(6, 5),
            )
            style.map(
                "TEntry",
                bordercolor=[("focus", C["primary"])],
                lightcolor=[("focus", C["primary"])],
                darkcolor=[("focus", C["primary"])],
                fieldbackground=[("disabled", C["head_bg"])],
                foreground=[("disabled", C["text_muted"])],
            )
            style.configure(
                "TCombobox",
                fieldbackground=C["card"],
                background=C["card"],
                bordercolor=C["border_input"],
                lightcolor=C["card"],
                darkcolor=C["card"],
                foreground=C["text"],
                arrowcolor=C["text_sub"],
                padding=(6, 4),
            )
            style.map(
                "TCombobox",
                fieldbackground=[("readonly", C["card"]), ("disabled", C["head_bg"])],
                foreground=[("disabled", C["text_muted"])],
                bordercolor=[("focus", C["primary"])],
                lightcolor=[("focus", C["primary"])],
                darkcolor=[("focus", C["primary"])],
                arrowcolor=[("active", C["primary"])],
            )
            # 下拉弹层是原生 Listbox，需通过 Tk 选项数据库着色
            self.root.option_add("*TCombobox*Listbox.background", C["card"])
            self.root.option_add("*TCombobox*Listbox.foreground", C["text"])
            self.root.option_add("*TCombobox*Listbox.selectBackground", C["primary"])
            self.root.option_add("*TCombobox*Listbox.selectForeground", C["on_primary"])

            # ---------- 进度条 ----------
            style.configure(
                "Horizontal.TProgressbar",
                troughcolor=C["head_bg"],
                background=C["primary"],
                thickness=8,
                borderwidth=0,
                lightcolor=C["primary"],
                darkcolor=C["primary"],
            )

            # ---------- 滚动条（细样式） ----------
            style.configure(
                "TScrollbar",
                background=C["thumb"],
                troughcolor=C["head_bg"],
                bordercolor=C["head_bg"],
                arrowsize=12,
                relief="flat",
            )
            style.map(
                "TScrollbar",
                background=[("active", C["thumb_hover"]), ("pressed", C["thumb_press"])],
            )
            try:
                # 部分主题不支持 width，单独包裹避免影响其余样式
                style.configure("Vertical.TScrollbar", width=8)
                style.configure("Horizontal.TScrollbar", width=8)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001 - 样式配置失败不影响功能
            print("[DevBox] 警告：界面样式未完全生效。（%s）" % exc)

    def _recolor_widgets(self):
        """对无法通过 ttk 样式着色的原生控件重新着色（主题切换时调用）。

        ttk 样式已由 ``_apply_styles`` 统一处理；此处只处理 Listbox、分隔线、
        强调条与 Treeview tag 等依赖显式颜色的控件。整段优雅降级。
        """
        C = self.palette
        try:
            self.root.configure(background=C["page"])
        except tk.TclError:
            return

        try:
            self.tool_listbox.configure(
                background=C["card"],
                foreground=C["text"],
                selectbackground=C["primary_light"],
                selectforeground=C["primary"],
            )
        except (tk.TclError, AttributeError):
            pass
        for line in self._border_lines:
            try:
                line.configure(background=C["border"])
            except tk.TclError:
                pass
        for bar in self._accent_bars:
            try:
                bar.configure(background=C["primary"])
            except tk.TclError:
                pass

        # 表格语义 tag（当前版本 / 斑马纹）颜色随主题更新
        try:
            self.installed_tree.tag_configure(
                "current",
                background=C["success_bg"],
                foreground=C["success"],
                font=_font(self.font_family, 10, True),
            )
            self.installed_tree.tag_configure("zebra", background=C["zebra"])
        except (tk.TclError, AttributeError):
            pass

        # 设置页主题下拉框同步当前主题（程序化切换时保持 UI 一致）
        try:
            label = THEME_LABELS.get(self.theme_name)
            if label and label in self._theme_labels_list:
                self.theme_combo.current(self._theme_labels_list.index(label))
        except (tk.TclError, AttributeError, ValueError):
            pass

    def apply_theme(self, name, persist=True):
        """切换到指定主题：重应用 ttk 样式并重着色原生控件。

        ``persist=True`` 时把选择写入 box.json；非法主题名回退默认主题。
        """
        if name not in THEMES:
            name = DEFAULT_THEME
        self.theme_name = name
        self.palette = dict(THEMES[name])
        self._apply_styles()
        self._recolor_widgets()
        if persist:
            try:
                self.box.data["theme"] = name
                self.box.save()
            except Exception as exc:  # noqa: BLE001 - 持久化失败不影响界面
                print("[DevBox] 警告：保存主题设置失败：%s" % exc)

    def _center_window(self, width, height):
        """把窗口设为指定尺寸并居中显示（装饰性，失败仅回退默认尺寸）。"""
        try:
            self.root.update_idletasks()
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            w = min(width, max(screen_w - 40, 600))
            h = min(height, max(screen_h - 80, 400))
            x = max((screen_w - w) // 2, 0)
            y = max((screen_h - h) // 2, 0)
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:  # noqa: BLE001 - 几何设置失败不影响功能
            pass

    # ------------------------------------------------------------------ #
    # 界面构建
    # ------------------------------------------------------------------ #

    def _show_about(self):
        """弹出「关于」：展示版本号、开发者与许可证/版权信息。"""
        messagebox.showinfo(
            "关于",
            "%s\n\n版本：%s\n开发者：%s\n许可证：%s\n%s\n\n"
            "数据目录：D:\\环境管理\\data\n本程序开源，允许研究与自用，禁止商业化使用。"
            % (APP_NAME, APP_VERSION, APP_AUTHOR, APP_LICENSE, APP_COPYRIGHT),
            parent=self.root,
        )

    def _build_ui(self):
        """搭建顶部标题区、左右分栏布局与四个标签页。"""
        # 状态栏变量需先于各标签页创建，供刷新时引用
        self.status_var = tk.StringVar()
        self.dl_status_var = tk.StringVar()
        self.ver_var = tk.StringVar()
        self.progress_pct_var = tk.StringVar(value="0%")

        self.root.configure(background=self.palette["page"])
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # 顶部标题区：主色强调条 + 大字号产品名 + 副标题，底部 1px 分隔线
        header = ttk.Frame(self.root, style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        title_row = ttk.Frame(header, style="Header.TFrame", padding=(16, 12))
        title_row.pack(side="top", fill="x")

        # 品牌强调条：竖向主色块，切换主题时同步重着色
        accent = tk.Frame(
            title_row, width=4, height=26, background=self.palette["primary"]
        )
        accent.pack(side="left", padx=(0, 10))
        accent.pack_propagate(False)
        self._accent_bars.append(accent)

        ttk.Label(title_row, text="环境管理", style="Title.TLabel").pack(
            side="left"
        )
        ttk.Label(
            title_row, text="多版本开发环境管理器", style="Subtitle.TLabel"
        ).pack(side="left", padx=(12, 0), pady=(6, 0))
        # 右侧：版本号 + 「关于」入口（展示版本与许可证/版权）
        ttk.Button(
            title_row, text="关于", style="TButton", command=self._show_about
        ).pack(side="right")
        ttk.Label(title_row, text="v" + APP_VERSION, style="Subtitle.TLabel").pack(
            side="right", padx=(0, 10), pady=(6, 0)
        )
        sep = tk.Frame(header, height=1, background=self.palette["border"])
        sep.pack(side="top", fill="x")
        self._border_lines.append(sep)

        main = ttk.Frame(self.root, padding=12)
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # 左侧工具列表
        left = ttk.LabelFrame(main, text="开发环境", padding=12)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        left.rowconfigure(0, weight=1)

        self.tool_listbox = tk.Listbox(
            left,
            exportselection=False,
            width=13,
            activestyle="none",
            background=self.palette["card"],
            foreground=self.palette["text"],
            selectbackground=self.palette["primary_light"],
            selectforeground=self.palette["primary"],
            highlightthickness=0,
            borderwidth=0,
            relief="flat",
            font=_font(self.font_family, 11),
        )
        self.tool_listbox.grid(row=0, column=0, sticky="ns")
        for tool in TOOL_ORDER:
            self.tool_listbox.insert("end", TOOL_NAMES.get(tool, tool))
        self.tool_listbox.bind("<<ListboxSelect>>", self._on_tool_select)

        tool_sb = ttk.Scrollbar(
            left, orient="vertical", command=self.tool_listbox.yview
        )
        tool_sb.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self.tool_listbox.configure(yscrollcommand=tool_sb.set)

        # 右侧标签页
        self.notebook = ttk.Notebook(main)
        self.notebook.grid(row=0, column=1, sticky="nsew")

        self._build_installed_tab()
        self._build_download_tab()
        self._build_settings_tab()
        self._build_mysql_tab()
        self._build_redis_tab()

        # 底部状态栏：上边框 1px + 浅灰底，展示当前选中工具的当前版本
        status_wrap = ttk.Frame(self.root, style="Status.TFrame")
        status_wrap.grid(row=2, column=0, sticky="ew")
        sep2 = tk.Frame(status_wrap, height=1, background=self.palette["border"])
        sep2.pack(side="top", fill="x")
        self._border_lines.append(sep2)
        status = ttk.Label(
            status_wrap,
            textvariable=self.status_var,
            anchor="w",
            style="Status.TLabel",
        )
        status.pack(side="top", fill="x")

    def _build_installed_tab(self):
        """「已安装版本」标签页。"""
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="已安装版本")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.installed_tree = ttk.Treeview(
            frame,
            columns=("ver", "source", "path"),
            show="headings",
            selectmode="browse",
        )
        self.installed_tree.heading("ver", text="版本")
        self.installed_tree.heading("source", text="来源")
        self.installed_tree.heading("path", text="路径")
        self.installed_tree.column("ver", width=130, anchor="w")
        self.installed_tree.column("source", width=90, anchor="center")
        self.installed_tree.column("path", width=300, anchor="w")
        # 当前激活版本：浅绿底 + 成功色文字加粗；其余行斑马纹
        self.installed_tree.tag_configure(
            "current",
            background=self.palette["success_bg"],
            foreground=self.palette["success"],
            font=_font(self.font_family, 10, True),
        )
        self.installed_tree.tag_configure("zebra", background=self.palette["zebra"])
        self.installed_tree.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(
            frame, orient="vertical", command=self.installed_tree.yview
        )
        sb.grid(row=0, column=1, sticky="ns")
        self.installed_tree.configure(yscrollcommand=sb.set)
        # 双击行 = 设为当前，缩短操作路径
        self.installed_tree.bind("<Double-1>", lambda e: self._set_current())

        # 空状态提示：列表为空时给出引导，有数据时自动隐藏
        self.installed_empty_hint = ttk.Label(
            frame,
            text="暂无已安装版本——可点击「扫描系统环境」或「下载新版本」",
            style="Hint.TLabel",
        )
        self.installed_empty_hint.grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )

        btns = ttk.Frame(frame)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        # 快速切换当前版本：下拉选择即生效，免去“选中行+点按钮”两步操作
        self.current_ver_var = tk.StringVar()
        ttk.Label(btns, text="当前版本：").pack(side="left")
        self.current_ver_combo = ttk.Combobox(
            btns, state="readonly", width=16, textvariable=self.current_ver_var
        )
        self.current_ver_combo.pack(side="left", padx=(0, 6))
        self.current_ver_combo.bind("<<ComboboxSelected>>", self._on_quick_switch)
        # 主操作按钮在后，按钮间距统一 6px
        ttk.Button(
            btns,
            text="设为当前",
            style="Primary.TButton",
            command=self._set_current,
        ).pack(side="left")
        ttk.Button(btns, text="导入本地 zip", command=self._import_zip).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(
            btns,
            text="卸载版本",
            style="Danger.TButton",
            command=self._uninstall_version,
        ).pack(side="left", padx=(6, 0))
        # 扫描系统已安装环境（只读扫描，注册进 DevBox 统一管理）
        self.scan_btn = ttk.Button(
            btns, text="扫描系统环境", command=self._scan_system
        )
        self.scan_btn.pack(side="left", padx=(6, 0))

    def _build_download_tab(self):
        """「下载新版本」标签页。"""
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="下载新版本")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.versions_tree = ttk.Treeview(
            frame,
            columns=("ver", "size"),
            show="headings",
            selectmode="browse",
        )
        self.versions_tree.heading("ver", text="版本")
        self.versions_tree.heading("size", text="大小")
        self.versions_tree.column("ver", width=220, anchor="w")
        self.versions_tree.column("size", width=120, anchor="e")
        self.versions_tree.grid(row=0, column=0, sticky="nsew")
        self.versions_tree.bind("<<TreeviewSelect>>", self._on_version_select)

        sb = ttk.Scrollbar(
            frame, orient="vertical", command=self.versions_tree.yview
        )
        sb.grid(row=0, column=1, sticky="ns")
        self.versions_tree.configure(yscrollcommand=sb.set)

        # 空状态提示：版本列表为空时给出引导，有数据时自动隐藏
        self.versions_empty_hint = ttk.Label(
            frame,
            text="点击「刷新版本列表」获取可用版本",
            style="Hint.TLabel",
        )
        self.versions_empty_hint.grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )

        form = ttk.Frame(frame)
        form.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="版本号：").grid(
            row=0, column=0, sticky="e", padx=(0, 6)
        )
        ttk.Entry(form, textvariable=self.ver_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 6)
        )
        # 主操作按钮在前，按钮间距统一 6px
        self.download_btn = ttk.Button(
            form,
            text="下载",
            style="Primary.TButton",
            command=self._download,
        )
        self.download_btn.grid(row=0, column=2, padx=(0, 6))
        self.refresh_btn = ttk.Button(
            form, text="刷新版本列表", command=self._refresh_versions
        )
        self.refresh_btn.grid(row=0, column=3)

        # 进度条 + 右侧百分比数字，视觉更清晰
        prog_row = ttk.Frame(frame)
        prog_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        prog_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(
            prog_row, mode="determinate", maximum=100
        )
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            prog_row,
            textvariable=self.progress_pct_var,
            width=5,
            anchor="e",
            style="Sub.TLabel",
        ).grid(row=0, column=1, padx=(8, 0))

        ttk.Label(
            frame,
            textvariable=self.dl_status_var,
            anchor="w",
            style="Sub.TLabel",
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def _build_settings_tab(self):
        """「设置」标签页：展示并允许修改 runtime_dir / shim_dir。"""
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="设置")
        frame.columnconfigure(1, weight=1)
        # 标签列定宽 + 右对齐，保证输入框左边缘对齐
        frame.columnconfigure(0, minsize=88)

        self.runtime_var = tk.StringVar(value=self.box.data.get("runtime_dir", ""))
        self.shim_var = tk.StringVar(value=self.box.data.get("shim_dir", ""))

        ttk.Label(frame, text="界面主题：").grid(
            row=0, column=0, sticky="e", padx=(0, 8), pady=4
        )
        # 主题下拉框：可读标签 -> 主题 key，选择即切换并持久化
        self.theme_combo = ttk.Combobox(
            frame,
            state="readonly",
            values=[THEME_LABELS[key] for key in THEMES],
            width=12,
        )
        self._theme_labels_list = [THEME_LABELS[key] for key in THEMES]
        self.theme_combo.current(self._theme_labels_list.index(THEME_LABELS[self.theme_name]))
        self.theme_combo.bind("<<ComboboxSelected>>", self._on_theme_change)
        self.theme_combo.grid(row=0, column=1, sticky="w", pady=4)

        # 下载源：国内镜像优先（失败自动回退官方）/ 仅官方源；选择即持久化
        self._mirror_options = [
            ("国内镜像（优先，自动回退官方）", "cn"),
            ("官方源", "default"),
        ]
        ttk.Label(frame, text="下载源：").grid(
            row=1, column=0, sticky="e", padx=(0, 8), pady=4
        )
        self.mirror_combo = ttk.Combobox(
            frame,
            state="readonly",
            values=[label for label, _ in self._mirror_options],
            width=28,
        )
        cur_mirror = str(self.box.data.get("mirror", "cn"))
        self._mirror_combo_index = {
            key: i for i, (_, key) in enumerate(self._mirror_options)
        }
        self.mirror_combo.current(
            self._mirror_combo_index.get(cur_mirror, 0)
        )
        self.mirror_combo.bind("<<ComboboxSelected>>", self._on_mirror_change)
        self.mirror_combo.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="运行时目录：").grid(
            row=2, column=0, sticky="e", padx=(0, 8), pady=4
        )
        ttk.Entry(frame, textvariable=self.runtime_var).grid(
            row=2, column=1, sticky="ew", pady=4
        )
        ttk.Label(frame, text="Shim 目录：").grid(
            row=3, column=0, sticky="e", padx=(0, 8), pady=4
        )
        ttk.Entry(frame, textvariable=self.shim_var).grid(
            row=3, column=1, sticky="ew", pady=4
        )
        ttk.Button(frame, text="保存", command=self._save_settings).grid(
            row=4, column=1, sticky="e", pady=(12, 0)
        )

    def _on_mirror_change(self, event=None):
        """下载源下拉变更：把展示名映射回 key 并持久化到配置。"""
        try:
            label = self.mirror_combo.get()
        except (tk.TclError, AttributeError):
            return
        key = next((k for l, k in self._mirror_options if l == label), None)
        if key:
            self.box.data["mirror"] = key
            self.box.save()

    def _on_theme_change(self, event=None):
        """设置页主题下拉框变更：把展示名映射回主题 key 并切换。"""
        try:
            label = self.theme_combo.get()
            for key, disp in THEME_LABELS.items():
                if disp == label:
                    self.apply_theme(key)
                    break
        except (tk.TclError, AttributeError):
            pass

    def _build_mysql_tab(self):
        """「MySQL 服务」标签页：服务端实例管理（仅对 MySQL 生效）。"""
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="MySQL 服务")
        frame.columnconfigure(1, weight=1)
        # 标签列定宽 + 右对齐，与设置页保持一致
        frame.columnconfigure(0, minsize=88)

        # 版本相关显示变量
        self.mysql_name_var = tk.StringVar(value="-")
        self.mysql_port_var = tk.StringVar(value="-")
        self.mysql_datadir_var = tk.StringVar(value="-")
        self.mysql_state_var = tk.StringVar(value="-")
        self.mysql_pwd_var = tk.StringVar(value="-")
        self.mysql_log_var = tk.StringVar(value="")
        self.mysql_btns = []
        self._mysql_busy = False

        # 警告提示条：警告浅底 + 深黄文字
        ttk.Label(
            frame,
            text="仅对左侧选中 MySQL 时可用；服务操作需要管理员权限。",
            style="Warn.TLabel",
            wraplength=520,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        rows = [
            ("服务名：", self.mysql_name_var),
            ("端口：", self.mysql_port_var),
            ("数据目录：", self.mysql_datadir_var),
            ("服务状态：", self.mysql_state_var),
            ("临时密码：", self.mysql_pwd_var),
        ]
        for i, (text, var) in enumerate(rows, start=1):
            ttk.Label(frame, text=text).grid(
                row=i, column=0, sticky="e", padx=(0, 8), pady=3
            )
            ttk.Label(
                frame, textvariable=var, anchor="w", wraplength=420, justify="left"
            ).grid(row=i, column=1, sticky="ew", pady=3)

        btns = ttk.Frame(frame)
        btns.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        # 「卸载服务」为危险操作用 Danger 样式，其余用默认；按钮间距统一 6px
        specs = [
            ("初始化实例", self._mysql_initialize, ""),
            ("安装服务", self._mysql_install, ""),
            ("启动", self._mysql_start, ""),
            ("停止", self._mysql_stop, ""),
            ("卸载服务", self._mysql_uninstall, "Danger.TButton"),
            ("刷新状态", self._mysql_refresh, ""),
            ("查看临时密码", self._mysql_show_password, ""),
        ]
        for text, command, style_name in specs:
            # 本行按钮较多，单独用紧凑内边距避免溢出裁剪
            button = ttk.Button(
                btns,
                text=text,
                style=style_name or "TButton",
                padding=(8, 4),
                command=command,
            )
            button.pack(side="left", padx=(0, 6))
            self.mysql_btns.append(button)

        # 第二行：密码与用户管理操作
        btns2 = ttk.Frame(frame)
        btns2.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        specs2 = [
            ("修改root密码", self._mysql_change_pwd),
            ("重置root密码", self._mysql_reset_pwd),
            ("创建用户", self._mysql_create_user),
        ]
        for text, command in specs2:
            button = ttk.Button(
                btns2, text=text, style="TButton", padding=(8, 4), command=command
            )
            button.pack(side="left", padx=(0, 6))
            self.mysql_btns.append(button)

        ttk.Label(
            frame,
            textvariable=self.mysql_log_var,
            anchor="w",
            style="Sub.TLabel",
            wraplength=520,
            justify="left",
        ).grid(row=len(rows) + 3, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        # 在「已安装版本」中切换选中项时，同步刷新本页显示
        self.installed_tree.bind("<<TreeviewSelect>>", self._on_installed_select, add="+")

    def _build_redis_tab(self):
        """「Redis 服务」标签页：服务端实例管理（仅对 Redis 生效）。"""
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Redis 服务")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(0, minsize=88)

        self.redis_name_var = tk.StringVar(value="-")
        self.redis_port_var = tk.StringVar(value="-")
        self.redis_state_var = tk.StringVar(value="-")
        self.redis_mem_var = tk.StringVar(value="-")
        self.redis_pwd_var = tk.StringVar(value="-")
        self.redis_log_var = tk.StringVar(value="")
        self.redis_btns = []
        self._redis_busy = False

        ttk.Label(
            frame,
            text="仅对左侧选中 Redis 时可用；服务操作需要管理员权限。",
            style="Warn.TLabel",
            wraplength=520,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        rows = [
            ("服务名：", self.redis_name_var),
            ("端口：", self.redis_port_var),
            ("服务状态：", self.redis_state_var),
            ("内存上限：", self.redis_mem_var),
            ("访问密码：", self.redis_pwd_var),
        ]
        for i, (text, var) in enumerate(rows, start=1):
            ttk.Label(frame, text=text).grid(
                row=i, column=0, sticky="e", padx=(0, 8), pady=3
            )
            ttk.Label(
                frame, textvariable=var, anchor="w", wraplength=420, justify="left"
            ).grid(row=i, column=1, sticky="ew", pady=3)

        btns = ttk.Frame(frame)
        btns.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        specs = [
            ("安装服务", self._redis_install, ""),
            ("启动", self._redis_start, ""),
            ("停止", self._redis_stop, ""),
            ("卸载服务", self._redis_uninstall, "Danger.TButton"),
            ("刷新状态", self._redis_refresh, ""),
        ]
        for text, command, style_name in specs:
            button = ttk.Button(
                btns, text=text, style=style_name or "TButton",
                padding=(8, 4), command=command,
            )
            button.pack(side="left", padx=(0, 6))
            self.redis_btns.append(button)

        btns2 = ttk.Frame(frame)
        btns2.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        specs2 = [
            ("设置内存上限", self._redis_set_mem),
            ("设置访问密码", self._redis_set_pwd),
        ]
        for text, command in specs2:
            button = ttk.Button(
                btns2, text=text, style="TButton", padding=(8, 4), command=command
            )
            button.pack(side="left", padx=(0, 6))
            self.redis_btns.append(button)

        ttk.Label(
            frame,
            textvariable=self.redis_log_var,
            anchor="w",
            style="Sub.TLabel",
            wraplength=520,
            justify="left",
        ).grid(row=len(rows) + 3, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    # ------------------------------------------------------------------ #
    # MySQL 服务页刷新
    # ------------------------------------------------------------------ #

    def _on_installed_select(self, event=None):
        """「已安装版本」选中项变化时，刷新 MySQL / Redis 服务页。"""
        self._refresh_mysql_tab()
        self._refresh_redis_tab()

    def _refresh_mysql_tab(self):
        """刷新 MySQL 服务页的版本相关显示与控件状态。"""
        enabled = self.tool == "mysql" and not self._mysql_busy
        for button in self.mysql_btns:
            button.config(state=("normal" if enabled else "disabled"))

        ver = self._selected_tree_ver(self.installed_tree) if enabled else None
        if self.tool != "mysql":
            self._set_mysql_display("-", "-", "-", "-", "-")
            self.mysql_log_var.set("请先在左侧选中 MySQL。")
            return
        if not ver:
            self._set_mysql_display("-", "-", "-", "-", "-")
            self.mysql_log_var.set("请在「已安装版本」中选择一个 MySQL 版本。")
            return

        name = mysqlsvc.service_name(ver)
        try:
            port = str(mysqlsvc.instance_port(self.box, ver))
        except Exception:  # noqa: BLE001
            port = "-"
        record = self.box.find_installed("mysql", ver) or {}
        root = record.get("path", "")
        datadir = str(Path(root) / "data") if root else "-"
        self._set_mysql_display(name, port, datadir, "查询中...", "-")

        # sc query 可能较慢，放到子线程执行，结果回主线程显示
        threading.Thread(
            target=self._mysql_query_status_worker, args=(ver,), daemon=True
        ).start()

    def _set_mysql_display(self, name, port, datadir, state, pwd):
        """一次性设置本页各显示变量。"""
        self.mysql_name_var.set(name)
        self.mysql_port_var.set(port)
        self.mysql_datadir_var.set(datadir)
        self.mysql_state_var.set(state)
        self.mysql_pwd_var.set(pwd)

    def _mysql_query_status_worker(self, ver):
        """子线程：查询服务状态。"""
        try:
            status = mysqlsvc.service_status(self.box, ver)
        except Exception:  # noqa: BLE001
            status = "未知"
        self._post(self._apply_mysql_status, ver, status)

    def _apply_mysql_status(self, ver, status):
        """主线程：回填服务状态（校验工具与选中项未被切换）。"""
        if self.tool != "mysql":
            return
        if self._selected_tree_ver(self.installed_tree) != ver:
            return
        self.mysql_state_var.set(status)

    # ------------------------------------------------------------------ #
    # MySQL 服务操作
    # ------------------------------------------------------------------ #

    def _mysql_get_ver(self):
        """获取当前应操作的 MySQL 版本；不可用时弹窗提示并返回 None。"""
        if self.tool != "mysql":
            messagebox.showinfo("提示", "请先在左侧选中 MySQL。", parent=self.root)
            return None
        ver = self._selected_tree_ver(self.installed_tree)
        if not ver:
            messagebox.showinfo(
                "提示",
                "请先在「已安装版本」中选择一个 MySQL 版本。",
                parent=self.root,
            )
            return None
        return ver

    def _mysql_run(self, action):
        """在主线程取版本后，把耗时操作放入子线程执行。"""
        ver = self._mysql_get_ver()
        if not ver:
            return
        self._mysql_busy = True
        self._refresh_mysql_tab()
        self.mysql_log_var.set("操作进行中...")
        threading.Thread(target=self._mysql_worker, args=(action, ver), daemon=True).start()

    def _mysql_worker(self, action, ver):
        """子线程：执行 mysqlsvc 中的动作函数，过程文本经 _post 回主线程。"""

        def log_cb(text):
            self._post(self.mysql_log_var.set, str(text))

        try:
            result = action(self.box, ver, log_cb)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "message": "操作失败：%s" % exc}
        self._post(self._mysql_done, ver, result)

    def _mysql_done(self, ver, result):
        """主线程：展示结果、恢复控件并刷新显示。"""
        self._mysql_busy = False
        self._refresh_mysql_tab()

        message = result.get("message", "")
        self.mysql_log_var.set(message)

        password = result.get("password")
        if password:
            self.mysql_pwd_var.set(password)

        if result.get("ok"):
            messagebox.showinfo("MySQL 服务", message, parent=self.root)
        else:
            messagebox.showerror("MySQL 服务", message, parent=self.root)

    def _mysql_initialize(self):
        """初始化实例。"""
        self._mysql_run(mysqlsvc.initialize)

    def _mysql_install(self):
        """安装 Windows 服务。"""
        self._mysql_run(mysqlsvc.install_service)

    def _mysql_start(self):
        """启动服务。"""
        self._mysql_run(mysqlsvc.start_service)

    def _mysql_stop(self):
        """停止服务。"""
        self._mysql_run(mysqlsvc.stop_service)

    def _mysql_uninstall(self):
        """卸载 Windows 服务。"""
        self._mysql_run(mysqlsvc.uninstall_service)

    def _mysql_refresh(self):
        """刷新本页显示与服务状态。"""
        self._refresh_mysql_tab()

    def _mysql_show_password(self):
        """从错误日志读取并展示临时密码：专用窗口，可一键复制。"""
        ver = self._mysql_get_ver()
        if not ver:
            return
        password = mysqlsvc.read_temp_password(self.box, ver)
        if password:
            self.mysql_pwd_var.set(password)
            _TempPasswordDialog(self, password)
        else:
            self.mysql_pwd_var.set("未找到")
            messagebox.showinfo(
                "临时密码", "未在错误日志中找到临时密码。", parent=self.root
            )

    def _mysql_change_pwd(self):
        """修改 root 密码（需知道当前密码且服务已启动）：单对话框，密码可切换显示。"""
        ver = self._mysql_get_ver()
        if not ver:
            return
        dlg = _FormDialog(self, "修改 root 密码", [
            ("old", "当前 root 密码：", "password"),
            ("new", "新 root 密码：", "password"),
            ("confirm", "确认新密码：", "password"),
        ])
        if dlg.result is None:
            return
        old = dlg.result["old"] or ""
        new = dlg.result["new"] or ""
        if not new:
            messagebox.showerror("错误", "新密码不能为空。", parent=self.root)
            return
        if new != dlg.result["confirm"]:
            messagebox.showerror("错误", "两次输入的新密码不一致。", parent=self.root)
            return
        self._mysql_run(
            lambda b, v, cb: mysqlsvc.set_root_password(b, v, new, old, cb)
        )

    def _mysql_reset_pwd(self):
        """重置 root 密码（无需旧密码，会短暂重启服务）：单对话框，密码可切换显示。"""
        ver = self._mysql_get_ver()
        if not ver:
            return
        if not messagebox.askyesno(
            "重置 root 密码",
            "重置会临时停止并重启该版本的 MySQL 服务（会中断现有连接）。确定继续？",
            parent=self.root,
        ):
            return
        dlg = _FormDialog(self, "重置 root 密码", [
            ("new", "新 root 密码：", "password"),
            ("confirm", "确认新密码：", "password"),
        ])
        if dlg.result is None:
            return
        new = dlg.result["new"] or ""
        if not new:
            messagebox.showerror("错误", "新密码不能为空。", parent=self.root)
            return
        if new != dlg.result["confirm"]:
            messagebox.showerror("错误", "两次输入的新密码不一致。", parent=self.root)
            return
        self._mysql_run(
            lambda b, v, cb: mysqlsvc.reset_root_password(b, v, new, cb)
        )

    def _mysql_create_user(self):
        """创建数据库用户（可选授权某个库）：单对话框一次输入，密码可切换显示。"""
        ver = self._mysql_get_ver()
        if not ver:
            return
        dlg = _FormDialog(self, "创建用户", [
            ("user", "用户名：", "text"),
            ("host", "主机：", "text", "localhost"),
            ("pwd", "用户密码：", "password"),
            ("grant", "授权数据库名（可留空）：", "text"),
            ("root_pwd", "root 密码（用于连接执行创建）：", "password"),
        ])
        if dlg.result is None:
            return
        user_v = (dlg.result["user"] or "").strip()
        if not user_v:
            messagebox.showerror("错误", "用户名不能为空。", parent=self.root)
            return
        host_v = (dlg.result["host"] or "localhost").strip()
        pwd = dlg.result["pwd"] or ""
        grant_v = (dlg.result["grant"] or "").strip()
        root_pwd = dlg.result["root_pwd"] or ""
        self._mysql_run(
            lambda b, v, cb: mysqlsvc.create_user(
                b, v, user_v, host_v, pwd, grant_v, root_pwd, cb
            )
        )

    def _mysql_restart_on_switch(self, old_ver, new_ver):
        """切换 MySQL 版本后：停旧服务、清理残留进程、启动新服务（后台执行）。"""
        self._mysql_busy = True
        self._refresh_mysql_tab()
        self.mysql_log_var.set("切换 MySQL 版本：正在停止旧实例并启动新实例...")

        def worker():
            def log_cb(text):
                self._post(self.mysql_log_var.set, str(text))

            try:
                result = mysqlsvc.switch_active(self.box, old_ver, new_ver, log_cb)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "message": "切换 MySQL 服务失败：%s" % exc}
            self._post(self._mysql_done, new_ver, result)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Redis 服务页刷新
    # ------------------------------------------------------------------ #

    def _refresh_redis_tab(self):
        """刷新 Redis 服务页的版本相关显示与控件状态。"""
        enabled = self.tool == "redis" and not self._redis_busy
        for button in self.redis_btns:
            button.config(state=("normal" if enabled else "disabled"))

        ver = self._selected_tree_ver(self.installed_tree) if enabled else None
        if self.tool != "redis":
            self._set_redis_display("-", "-", "-", "-", "-")
            self.redis_log_var.set("请先在左侧选中 Redis。")
            return
        if not ver:
            self._set_redis_display("-", "-", "-", "-", "-")
            self.redis_log_var.set("请在「已安装版本」中选择一个 Redis 版本。")
            return

        name = redissvc.service_name(ver)
        try:
            port = str(redissvc.instance_port(self.box, ver))
        except Exception:  # noqa: BLE001
            port = "-"
        mem = redissvc.read_maxmemory_mb(self.box, ver)
        mem_txt = ("%d MB" % mem) if mem else "未限制"
        pwd_txt = "已设置" if redissvc.read_password(self.box, ver) else "未设置"
        self._set_redis_display(name, port, "查询中...", mem_txt, pwd_txt)

        threading.Thread(
            target=self._redis_query_status_worker, args=(ver,), daemon=True
        ).start()

    def _set_redis_display(self, name, port, state, mem, pwd):
        """一次性设置 Redis 页各显示变量。"""
        self.redis_name_var.set(name)
        self.redis_port_var.set(port)
        self.redis_state_var.set(state)
        self.redis_mem_var.set(mem)
        self.redis_pwd_var.set(pwd)

    def _redis_query_status_worker(self, ver):
        """子线程：查询 Redis 服务状态。"""
        try:
            status = redissvc.service_status(self.box, ver)
        except Exception:  # noqa: BLE001
            status = "未知"
        self._post(self._apply_redis_status, ver, status)

    def _apply_redis_status(self, ver, status):
        """主线程：回填 Redis 服务状态（校验工具与选中项未被切换）。"""
        if self.tool != "redis":
            return
        if self._selected_tree_ver(self.installed_tree) != ver:
            return
        self.redis_state_var.set(status)

    # ------------------------------------------------------------------ #
    # Redis 服务操作
    # ------------------------------------------------------------------ #

    def _redis_get_ver(self):
        """获取当前应操作的 Redis 版本；不可用时弹窗提示并返回 None。"""
        if self.tool != "redis":
            messagebox.showinfo("提示", "请先在左侧选中 Redis。", parent=self.root)
            return None
        ver = self._selected_tree_ver(self.installed_tree)
        if not ver:
            messagebox.showinfo(
                "提示", "请先在「已安装版本」中选择一个 Redis 版本。", parent=self.root
            )
            return None
        return ver

    def _redis_run(self, action):
        """在主线程取版本后，把耗时操作放入子线程执行。"""
        ver = self._redis_get_ver()
        if not ver:
            return
        self._redis_busy = True
        self._refresh_redis_tab()
        self.redis_log_var.set("操作进行中...")
        threading.Thread(target=self._redis_worker, args=(action, ver), daemon=True).start()

    def _redis_worker(self, action, ver):
        """子线程：执行 redissvc 中的动作函数，过程文本经 _post 回主线程。"""

        def log_cb(text):
            self._post(self.redis_log_var.set, str(text))

        try:
            result = action(self.box, ver, log_cb)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "message": "操作失败：%s" % exc}
        self._post(self._redis_done, ver, result)

    def _redis_done(self, ver, result):
        """主线程：展示结果、恢复控件并刷新显示。"""
        self._redis_busy = False
        self._refresh_redis_tab()
        message = result.get("message", "")
        self.redis_log_var.set(message)
        if result.get("ok"):
            messagebox.showinfo("Redis 服务", message, parent=self.root)
        else:
            messagebox.showerror("Redis 服务", message, parent=self.root)

    def _redis_install(self):
        self._redis_run(redissvc.install_service)

    def _redis_start(self):
        self._redis_run(redissvc.start_service)

    def _redis_stop(self):
        self._redis_run(redissvc.stop_service)

    def _redis_uninstall(self):
        self._redis_run(redissvc.uninstall_service)

    def _redis_refresh(self):
        self._refresh_redis_tab()

    def _redis_set_mem(self):
        """设置内存上限（MB）：写入 conf 后重启服务生效。"""
        ver = self._redis_get_ver()
        if not ver:
            return
        current = redissvc.read_maxmemory_mb(self.box, ver) or 256
        mb = simpledialog.askinteger(
            "设置内存上限",
            "内存上限（MB，正整数）：",
            initialvalue=current,
            minvalue=1,
            parent=self.root,
        )
        if not mb:
            return
        self._redis_run(lambda b, v, cb: redissvc.set_maxmemory(b, v, mb, cb))

    def _redis_set_pwd(self):
        """设置访问密码（requirepass）：单对话框，密码可切换显示；留空表示清除。"""
        ver = self._redis_get_ver()
        if not ver:
            return
        dlg = _FormDialog(self, "设置访问密码", [
            ("pwd", "新访问密码（留空清除）：", "password"),
            ("confirm", "确认密码：", "password"),
        ])
        if dlg.result is None:
            return
        pwd = dlg.result["pwd"] or ""
        if pwd:
            if pwd != dlg.result["confirm"]:
                messagebox.showerror("错误", "两次输入的密码不一致。", parent=self.root)
                return
        else:
            if not messagebox.askyesno(
                "设置访问密码", "确定清除访问密码吗？", parent=self.root
            ):
                return
        self._redis_run(lambda b, v, cb: redissvc.set_password(b, v, pwd, cb))

    def _redis_restart_on_switch(self, old_ver, new_ver):
        """切换 Redis 版本后：停旧服务、清理残留进程、启动新服务（后台执行）。"""
        self._redis_busy = True
        self._refresh_redis_tab()
        self.redis_log_var.set("切换 Redis 版本：正在停止旧实例并启动新实例...")

        def worker():
            def log_cb(text):
                self._post(self.redis_log_var.set, str(text))

            try:
                result = redissvc.switch_active(self.box, old_ver, new_ver, log_cb)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "message": "切换 Redis 服务失败：%s" % exc}
            self._post(self._redis_done, new_ver, result)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # 通用工具方法
    # ------------------------------------------------------------------ #

    def _post(self, func, *args):
        """把回调从子线程投递到主线程执行；窗口已销毁时静默忽略。"""
        try:
            self.root.after(0, func, *args)
        except tk.TclError:
            pass

    @staticmethod
    def _selected_tree_ver(tree):
        """返回 Treeview 中选中行第一列（版本号）；未选中返回 None。"""
        selection = tree.selection()
        if not selection:
            return None
        values = tree.item(selection[0], "values")
        return str(values[0]) if values else None

    def _refresh_status(self):
        """刷新底部状态栏中的当前工具与当前版本。"""
        name = TOOL_NAMES.get(self.tool, self.tool)
        current = self.box.current(self.tool)
        if current:
            self.status_var.set("工具：%s        当前版本：%s" % (name, current))
        else:
            self.status_var.set("工具：%s        当前版本：未设置" % name)

    def _update_empty_hints(self):
        """按列表是否为空切换空状态提示的显示（纯展示，不影响数据）。"""
        if self.installed_tree.get_children():
            self.installed_empty_hint.grid_remove()
        else:
            self.installed_empty_hint.grid()
        if self.versions_tree.get_children():
            self.versions_empty_hint.grid_remove()
        else:
            self.versions_empty_hint.grid()

    # ------------------------------------------------------------------ #
    # 左侧工具切换
    # ------------------------------------------------------------------ #

    def _on_tool_select(self, event=None):
        """工具列表选中事件。"""
        selection = self.tool_listbox.curselection()
        if selection:
            self._select_tool(selection[0])

    def _select_tool(self, index):
        """切换当前工具并刷新右侧三个标签页内容。"""
        if index < 0 or index >= len(TOOL_ORDER):
            return
        self.tool = TOOL_ORDER[index]

        self._refresh_installed()

        # 下载页复位：清空版本列表、输入框
        self.versions_tree.delete(*self.versions_tree.get_children())
        self.ver_var.set("")
        # 下载进行中时保留进度与状态显示，避免切换工具后“看不到进度”
        if not self._downloading:
            self._reset_progress_bar()
            self.progress_pct_var.set("0%")
            self.dl_status_var.set("")
        self._update_empty_hints()

        self._refresh_status()

    def _refresh_installed(self):
        """从 box.installed_list 重新加载已安装版本列表。"""
        tree = self.installed_tree
        tree.delete(*tree.get_children())
        current = self.box.current(self.tool)
        for index, record in enumerate(self.box.installed_list(self.tool)):
            ver = record.get("ver", "")
            # 当前版本用高亮 tag，其余奇数行斑马纹（二者不叠加，避免底色冲突）
            if ver == current:
                tags = ("current",)
            elif index % 2 == 1:
                tags = ("zebra",)
            else:
                tags = ()
            tree.insert(
                "",
                "end",
                values=(
                    ver,
                    _source_label(record.get("source", "")),
                    record.get("path", ""),
                ),
                tags=tags,
            )
        # 同步快速切换下拉框：列出当前工具所有已安装版本并回填激活版本
        try:
            vers = [str(r.get("ver", "")) for r in self.box.installed_list(self.tool)]
            self.current_ver_combo["values"] = vers
            self.current_ver_var.set(str(current or ""))
        except (tk.TclError, AttributeError):
            pass
        self._update_empty_hints()
        # 已安装列表变化后同步刷新 MySQL / Redis 服务页
        self._refresh_mysql_tab()
        self._refresh_redis_tab()

    # ------------------------------------------------------------------ #
    # 设为当前
    # ------------------------------------------------------------------ #

    def _select_tree_ver(self, ver):
        """在已安装列表中选中指定版本行；未找到返回 False。"""
        for iid in self.installed_tree.get_children():
            values = self.installed_tree.item(iid, "values")
            if values and str(values[0]) == str(ver):
                self.installed_tree.selection_set(iid)
                self.installed_tree.focus(iid)
                return True
        return False

    def _on_quick_switch(self, event=None):
        """快速切换下拉框变更：选中对应行并立即设为当前版本。"""
        ver = self.current_ver_var.get()
        if not ver or ver == self.box.current(self.tool):
            return
        if not self._select_tree_ver(ver):
            return
        self._set_current()

    def _set_current(self):
        """把选中的已安装版本设为当前版本，并重新生成 shim、同步环境变量。"""
        ver = self._selected_tree_ver(self.installed_tree)
        if not ver:
            messagebox.showinfo(
                "提示", "请先在列表中选择一个已安装版本。", parent=self.root
            )
            return

        self._check_maven_java_compat(ver)

        prev = self.box.current(self.tool)
        try:
            self.box.set_current(self.tool, ver)
            write_shims(self.box)
            sync_homes(self.box)
        except Exception as exc:  # noqa: BLE001 - 界面层需兜底提示
            messagebox.showerror(
                "错误", "切换当前版本失败：%s" % exc, parent=self.root
            )
            return

        self._refresh_installed()
        self._refresh_status()

        # 切换 MySQL 版本：停旧服务、清理残留进程、启动新服务，避免端口/实例冲突。
        # （其他工具无常驻服务进程，shim 切换即时生效，无需重启）
        if self.tool == "mysql" and prev and prev != ver:
            self._mysql_restart_on_switch(prev, ver)
        # 切换 Redis 版本：同理停旧启新
        if self.tool == "redis" and prev and prev != ver:
            self._redis_restart_on_switch(prev, ver)

    # ------------------------------------------------------------------ #
    # 下载新版本
    # ------------------------------------------------------------------ #

    def _on_version_select(self, event=None):
        """点击版本列表中的版本时，回填到版本号输入框。"""
        ver = self._selected_tree_ver(self.versions_tree)
        if ver:
            self.ver_var.set(ver)

    def _refresh_versions(self):
        """在子线程中获取版本列表，避免界面卡死。"""
        tool = self.tool
        query = self.ver_var.get().strip()

        self.refresh_btn.config(state="disabled")
        self.dl_status_var.set("正在获取版本列表...")
        threading.Thread(
            target=self._load_versions, args=(tool, query), daemon=True
        ).start()

    def _load_versions(self, tool, query):
        """子线程：调用 Provider.list_versions。"""
        try:
            items = PROVIDERS[tool].list_versions(query or None)
        except Exception:  # noqa: BLE001 - 网络/解析异常统一兜底
            items = []
        self._post(self._versions_loaded, tool, items)

    def _versions_loaded(self, tool, items):
        """主线程：回填版本列表。"""
        self.refresh_btn.config(state="normal")
        # 期间用户可能已切换工具，此时丢弃过期结果
        if tool != self.tool:
            return

        self.versions_tree.delete(*self.versions_tree.get_children())
        for item in items or []:
            ver = item.get("ver", "")
            size = item.get("size", 0)
            size_txt = _fmt_mb(size) if size else "-"
            self.versions_tree.insert("", "end", values=(ver, size_txt))
        self._update_empty_hints()

        if items:
            self.dl_status_var.set("共获取到 %d 个版本，可选择或手动输入版本号。" % len(items))
        else:
            self.dl_status_var.set("获取失败，可手动输入版本号。")

    def _download(self):
        """根据版本号输入框的内容发起下载（在子线程中执行）。"""
        tool = self.tool
        ver = self.ver_var.get().strip()
        if not ver:
            messagebox.showwarning(
                "提示", "请先选择或输入要下载的版本号。", parent=self.root
            )
            return

        mirror = self.box.data.get("mirror", "cn")
        urls = PROVIDERS[tool].build_urls(ver, mirror)
        if not urls:
            messagebox.showerror(
                "错误",
                "无法为版本 %s 构造下载地址，请检查版本号。" % ver,
                parent=self.root,
            )
            return

        self._check_maven_java_compat(ver)

        # 下载文件名从首选 URL 的 basename 提取（各镜像文件名一致）
        filename = Path(urlparse(urls[0]).path).name or ("%s-%s.zip" % (tool, ver))
        dest = Path(self.box.data["cache_dir"]) / filename
        target_dir = Path(self.box.data["runtime_dir"]) / tool / ver

        self.download_btn.config(state="disabled")
        self._downloading = True
        self._reset_progress_bar()
        self.dl_status_var.set("开始下载：%s" % filename)

        threading.Thread(
            target=self._download_worker,
            args=(tool, ver, urls, dest, target_dir),
            daemon=True,
        ).start()

    def _download_worker(self, tool, ver, urls, dest, target_dir):
        """子线程：多源下载 -> 解压 -> post_install -> 回主线程登记。"""
        try:
            download_any(urls, dest, cb=self._progress_cb)
            self._post(self.dl_status_var.set, "下载完成，正在解压...")
            unpack(dest, target_dir)
            PROVIDERS[tool].post_install(target_dir)
        except Exception as exc:  # noqa: BLE001 - 子线程异常统一转主线程提示
            self._post(self._download_failed, exc)
            return
        self._post(self._download_done, tool, ver, target_dir)

    def _progress_cb(self, downloaded, total):
        """下载进度回调（在子线程中被调用，转发到主线程）。"""
        self._post(self._update_progress, downloaded, total)

    def _set_window_icon(self):
        """把 Tk 默认羽毛图标替换为项目图标（标题栏 + 任务栏 + 对话框）。"""
        try:
            from app import resource_path

            ico = resource_path("devbox.ico")
            # default= 让所有派生窗口（含 messagebox）统一使用项目图标
            self.root.iconbitmap(default=str(ico))
        except Exception:  # noqa: BLE001 - 图标缺失/格式不支持时静默回退
            pass

    def _reset_progress_bar(self):
        """停止滚动动画并复位为确定模式进度条。"""
        try:
            self.progress.stop()
        except Exception:  # noqa: BLE001
            pass
        self.progress.config(mode="determinate")
        self.progress["value"] = 0

    def _update_progress(self, downloaded, total):
        """主线程：更新进度条与状态文字。

        部分镜像不返回 Content-Length（total<=0），确定模式进度条会看似卡住，
        此时改用 indeterminate 滚动动画明确表示“正在下载”。
        """
        if total and total > 0:
            pct = min(100.0, downloaded * 100.0 / total)
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.config(mode="determinate")
            self.progress["value"] = pct
            self.progress_pct_var.set("%.0f%%" % pct)
            self.dl_status_var.set(
                "下载中 %.1f%%（%s / %s）"
                % (pct, _fmt_mb(downloaded), _fmt_mb(total))
            )
        else:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.config(mode="indeterminate")
                self.progress.start(15)
            self.progress_pct_var.set("大小未知")
            self.dl_status_var.set(
                "下载中 %s（镜像未提供总大小）" % _fmt_mb(downloaded)
            )

    def _download_done(self, tool, ver, target_dir):
        """主线程：登记安装记录、设为当前、生成 shim 与同步环境变量。"""
        try:
            self.box.add_installed(tool, ver, target_dir, "download")
            self.box.set_current(tool, ver)
            write_shims(self.box)
            sync_homes(self.box)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "错误", "安装登记失败：%s" % exc, parent=self.root
            )

        self._downloading = False
        self.download_btn.config(state="normal")
        self._reset_progress_bar()
        self.progress["value"] = 100
        self.progress_pct_var.set("100%")
        self.dl_status_var.set(
            "下载并安装完成：%s %s" % (TOOL_NAMES.get(tool, tool), ver)
        )
        self._refresh_installed()
        self._refresh_status()

    def _download_failed(self, exc):
        """主线程：下载/解压失败提示并恢复按钮。"""
        self._downloading = False
        self.download_btn.config(state="normal")
        self._reset_progress_bar()
        self.dl_status_var.set("下载失败。")
        messagebox.showerror("下载失败", str(exc), parent=self.root)

    # ------------------------------------------------------------------ #
    # 导入本地 zip
    # ------------------------------------------------------------------ #

    def _import_zip(self):
        """导入本地 zip 压缩包为指定版本（先校验是否为对应工具的安装包）。"""
        tool = self.tool
        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择本地 zip 压缩包",
            filetypes=[("Zip", "*.zip"), ("All", "*.*")],
        )
        if not path:
            return

        # 1) 解压前先校验：合法 zip 且含当前工具的关键文件（避免装错包）
        markers = PROVIDERS[tool].install_markers
        try:
            validate_zip_package(path, markers)
        except ValueError as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.root)
            return

        default_ver = _guess_version_from_name(path)
        ver = simpledialog.askstring(
            "版本号",
            "请输入该压缩包对应的版本号：",
            initialvalue=default_ver,
            parent=self.root,
        )
        if not ver or not ver.strip():
            return
        ver = ver.strip()

        target_dir = Path(self.box.data["runtime_dir"]) / tool / ver
        try:
            unpack(path, target_dir)
            PROVIDERS[tool].post_install(target_dir)
        except ValueError as exc:
            # 路径遍历/损坏等校验失败：清理半成品，不登记
            shutil.rmtree(target_dir, ignore_errors=True)
            messagebox.showerror("导入失败", str(exc), parent=self.root)
            return
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(target_dir, ignore_errors=True)
            messagebox.showerror("导入失败", "解压失败：%s" % exc, parent=self.root)
            return

        # 2) 解压后再校验：目标目录确实含关键文件（防止空目录/结构异常）
        missing = [
            m for m in markers if not _installed_has_marker(target_dir, m)
        ]
        if missing:
            shutil.rmtree(target_dir, ignore_errors=True)
            messagebox.showerror(
                "导入失败",
                "解压后未找到 %s，该压缩包可能不是完整的 %s 安装包。"
                % ("、".join(missing), TOOL_NAMES.get(tool, tool)),
                parent=self.root,
            )
            return

        self.box.add_installed(tool, ver, target_dir, "import")
        self._refresh_installed()
        self._refresh_status()
        messagebox.showinfo(
            "导入成功",
            "已导入 %s %s。\n可在“已安装版本”中“设为当前”以启用。"
            % (TOOL_NAMES.get(tool, tool), ver),
            parent=self.root,
        )

    # ------------------------------------------------------------------ #
    # 扫描系统环境
    # ------------------------------------------------------------------ #

    def _scan_system(self):
        """扫描系统已安装环境并注册进 DevBox（子线程执行，不覆盖已有记录）。"""
        tool = self.tool
        self.scan_btn.config(state="disabled")
        self.status_var.set("正在扫描系统已安装环境...")
        threading.Thread(
            target=self._scan_system_worker, args=(tool,), daemon=True
        ).start()

    def _scan_system_worker(self, tool):
        """子线程：调用 systemscan.scan_system 扫描指定工具的系统安装。"""
        try:
            found = systemscan.scan_system(tool)
        except Exception as exc:  # noqa: BLE001 - 扫描异常统一转主线程提示
            self._post(self._scan_system_failed, exc)
            return
        self._post(self._scan_system_done, tool, found)

    def _scan_system_done(self, tool, found):
        """主线程：注册扫描结果（绝不覆盖已有记录）、刷新并汇报数量。"""
        self.scan_btn.config(state="normal")
        added = 0
        skipped = 0
        try:
            for record in (found or {}).get(tool, []) or []:
                ver = record.get("ver")
                path = record.get("path")
                if not ver or not path:
                    continue
                # 任何来源的同版本记录已存在时跳过，绝不覆盖
                if self.box.find_installed(tool, ver) is None:
                    self.box.add_installed(tool, ver, path, "system")
                    added += 1
                else:
                    skipped += 1
        except Exception as exc:  # noqa: BLE001
            self._refresh_installed()
            self._refresh_status()
            messagebox.showerror(
                "错误", "注册系统环境失败：%s" % exc, parent=self.root
            )
            return

        self._refresh_installed()
        self._refresh_status()
        name = TOOL_NAMES.get(tool, tool)
        messagebox.showinfo(
            "扫描完成",
            "%s：新注册 %d 个系统环境（%d 个已存在被跳过）。"
            % (name, added, skipped),
            parent=self.root,
        )

    def _scan_system_failed(self, exc):
        """主线程：扫描失败提示并恢复按钮。"""
        self.scan_btn.config(state="normal")
        self._refresh_status()
        messagebox.showerror("扫描失败", str(exc), parent=self.root)

    # ------------------------------------------------------------------ #
    # 卸载版本
    # ------------------------------------------------------------------ #

    def _uninstall_version(self):
        """卸载选中的已安装版本，并清理服务/进程/目录/缓存等残留。

        卸载前先扫描残留并向用户展示将被清理的内容；来源为 ``system`` 的
        记录只移除 DevBox 登记，绝不删除系统安装目录或系统服务。
        """
        ver = self._selected_tree_ver(self.installed_tree)
        if not ver:
            messagebox.showinfo(
                "提示", "请先在列表中选择要卸载的版本。", parent=self.root
            )
            return

        name = TOOL_NAMES.get(self.tool, self.tool)
        items, is_system = uninstall.scan_residuals(self.box, self.tool, ver)
        if is_system:
            prompt = (
                "确定从 DevBox 移除 %s %s 的记录吗？\n"
                "该版本来自系统安装，仅移除 DevBox 记录，不会删除系统安装目录或系统服务。"
                % (name, ver)
            )
        else:
            lines = ["确定卸载 %s %s 吗？将一并清理以下残留：" % (name, ver)]
            if items:
                lines += ["  · %s：%s" % (k, v) for k, v in items]
            else:
                lines.append("  · 版本登记")
            lines.append("此操作不可恢复。")
            prompt = "\n".join(lines)
        if not messagebox.askyesno("确认卸载", prompt, parent=self.root):
            return

        tool = self.tool
        self.scan_btn.config(state="disabled")
        self.status_var.set("正在卸载 %s %s ..." % (name, ver))

        def worker():
            def log_cb(text):
                self._post(self.status_var.set, str(text))

            try:
                result = uninstall.uninstall(self.box, tool, ver, log_cb)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "message": "卸载失败：%s" % exc, "was_current": False}
            self._post(self._uninstall_done, tool, ver, result)

        threading.Thread(target=worker, daemon=True).start()

    def _uninstall_done(self, tool, ver, result):
        """主线程：卸载完成后重写 shim（若卸的是激活版本）、刷新并汇报。"""
        self.scan_btn.config(state="normal")
        if result.get("was_current"):
            try:
                write_shims(self.box)
                sync_homes(self.box)
            except Exception:  # noqa: BLE001
                pass
        self._refresh_installed()
        self._refresh_status()
        self._refresh_mysql_tab()
        self._refresh_redis_tab()
        if result.get("ok"):
            messagebox.showinfo("卸载完成", result.get("message", ""), parent=self.root)
        else:
            messagebox.showerror("卸载失败", result.get("message", ""), parent=self.root)

    # ------------------------------------------------------------------ #
    # 设置
    # ------------------------------------------------------------------ #

    def _save_settings(self):
        """保存 runtime_dir / shim_dir 到配置并刷新相关显示。"""
        runtime_dir = self.runtime_var.get().strip()
        shim_dir = self.shim_var.get().strip()
        try:
            if runtime_dir:
                self.box.data["runtime_dir"] = runtime_dir
            if shim_dir:
                self.box.data["shim_dir"] = shim_dir
            self.box.save()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return

        # 回填规范化后的路径，并刷新状态栏
        self.runtime_var.set(self.box.data.get("runtime_dir", ""))
        self.shim_var.set(self.box.data.get("shim_dir", ""))
        self._refresh_status()
        messagebox.showinfo("已保存", "设置已保存。", parent=self.root)

    # ------------------------------------------------------------------ #
    # 版本兼容性启发式提示
    # ------------------------------------------------------------------ #

    def _check_maven_java_compat(self, ver):
        """Maven 4.x 需要 Java 11+，与当前 Java（如 8）明显不兼容时给出告警。"""
        if self.tool != "maven":
            return
        if not str(ver).startswith("4."):
            return
        java_major = _java_major(self.box.current("java"))
        if java_major is not None and java_major < 11:
            messagebox.showwarning(
                "版本兼容性提示",
                "Maven %s 需要 Java 11 及以上版本，当前 Java 主版本为 %s，"
                "可能无法正常运行。" % (ver, java_major),
                parent=self.root,
            )

    # ------------------------------------------------------------------ #
    # 启动
    # ------------------------------------------------------------------ #

    def run(self):
        """进入主事件循环；启动后按需弹出首次运行提示。"""
        notice = getattr(self.box, NOTICE_ATTR, None)
        if notice:
            self.root.after(
                300,
                lambda: messagebox.showinfo("环境管理", notice, parent=self.root),
            )
        self.root.mainloop()


def run_gui(box=None):
    """启动环境管理图形界面。

    数据统一存储在 ``D:\\环境管理\\data``，不占用 C 盘空间。
    ``box`` 为 None 时创建默认 ``Box()``。函数内部构建 ``App`` 并进入
    主事件循环，返回创建好的 ``App`` 实例（mainloop 结束后）。
    """
    if box is None:
        box = Box()
    app = App(box)
    app.run()
    return app
