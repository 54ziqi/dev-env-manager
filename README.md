# 开发环境管理器 (DevBox)

> Windows 多版本开发环境管理器 —— 图形化下载、安装、切换 **Java / Node.js / Python / PHP / Maven / MySQL / Redis**，并为 MySQL / Redis 提供多实例服务化管理。

- **开发者**：子柒
- **许可证**：[MIT](./LICENSE)
- **平台**：Windows 10 / 11（x64）
- **版本**：1.0.0
- **技术栈**：Python 3（仅标准库 + tkinter）· PyInstaller · NSIS · Authenticode 签名

🇬🇧 English version: [README.en.md](./README.en.md)

---

## 📥 下载

前往 [`release/`](./release/) 目录下载：

- **安装包**：`DevBox-Setup-<版本>.exe`（双击安装，可选目录）
- **免安装版**：`DevBox-<版本>.zip`（解压后双击 `DevBox.exe` 直接运行）

版本历史见 [release/CHANGELOG.md](./release/CHANGELOG.md)。产物均已 Authenticode 签名（发布者 CN=子柒）。

## ✨ 主要特性

- **多版本共存与秒级切换**：每个版本独立安装在数据目录，通过 `shim` 转发脚本 + `shim.cfg` 实现全局命令透明代理；切换版本只改写配置，无需重装或改 PATH。
- **国内镜像优先下载**：多源候选（国内镜像在前、官方源兜底），支持断点续传、自动重试，下载后对压缩包做「合法性 + 关键文件」双重校验，防止装错包/损坏包。
- **MySQL 服务化管理**：按版本创建独立实例（独立端口 / 服务名 / 数据目录 / 配置目录），支持初始化实例、安装/启动/停止/卸载服务、修改与重置 root 密码、创建用户、版本切换重启，并可从错误日志提取并一键复制初始临时密码。
- **Redis 服务化管理**：按版本独立实例，支持安装/启停/卸载服务、设置内存上限（`maxmemory`）与访问密码（`requirepass`）、版本切换。
- **彻底卸载 + 残留清理**：卸载版本时清理 Windows 服务、残留进程、安装目录、实例配置、下载缓存、激活标记与失效的用户环境变量（`*_HOME`），并在卸载前展示将被清理的残留清单。
- **运行时管理员权限**：启动即通过 UAC 申请管理员权限（清单 `requireAdministrator` + 运行时自提权双保障），确保服务安装/启停与系统 PATH 写入可用。
- **不占用 C 盘**：所有下载、解压版本、配置、日志统一存放在 `D:\环境管理\data`。
- **中文图形界面**：基于 tkinter，支持浅色 / 深色主题、自定义窗口图标、密码可见切换等 UX 细节。

## 🧰 支持的环境

| 环境 | 说明 | 服务化管理 |
|------|------|:--:|
| Java | Eclipse Temurin (Adoptium) | — |
| Node.js | nodejs.org 官方发行 | — |
| Python | Windows 嵌入式发行版 | — |
| PHP | Windows NTS 版 | — |
| Maven | Apache 官方归档 | — |
| MySQL | 社区版 winx64 zip | ✅ 多实例服务 + 初始化 + 密码管理 |
| Redis | Windows 版（tporadowski / MicrosoftArchive） | ✅ 多实例服务 + 内存 + 密码 |

## 🚀 快速开始

### 作为普通用户使用

1. 下载并运行 `DevBox-Setup.exe`，选择安装位置（建议非系统盘，如 `D:\DevBox`）。
2. 首次启动会弹出 UAC 请求管理员权限，请允许。
3. 在左侧选择环境 → 「下载新版本」刷新并下载 → 「已安装版本」中「设为当前」即可。
4. 新开一个命令行窗口，`java -version` / `node -v` / `mvn -v` 等命令即指向所选版本。

### 从源码运行

```bash
# 需要已安装 Python 3.8+（含 tkinter，Windows 官方安装包默认自带）
python devbox.py
```

> 运行本身零第三方依赖（仅标准库）；`requirements.txt` 中的 `pyinstaller` 仅用于打包。

## 🛠️ 构建与测试

```bash
pip install -r requirements.txt   # 仅打包需要（PyInstaller）

python build.py --clean           # 完整流水线：PyInstaller 打包 → 签名 → zip → NSIS 安装包 → 签名
python build.py --no-nsis         # 不生成 NSIS 安装包
python build.py --nsis-only       # 仅重新生成安装包（复用已有 dist 产物）

python -m unittest tests.test_devbox   # 运行单元测试
```

构建产物位于 `dist/`：`DevBox/`（绿色目录版）、`DevBox.zip`、`DevBox-Setup.exe`（已 Authenticode 签名，发布者 CN=子柒）。

## 🖥️ 界面说明

| 标签页 | 功能 |
|--------|------|
| 已安装版本 | 列出该环境已安装版本；「设为当前」/ 下拉快速切换 / 双击切换；导入本地 zip；扫描系统环境；卸载版本（含残留清理） |
| 下载新版本 | 刷新在线版本列表、下载并安装 |
| MySQL 服务 | 初始化实例、安装/启停/卸载服务、修改/重置 root 密码、创建用户、查看临时密码 |
| Redis 服务 | 安装/启停/卸载服务、设置内存上限、设置访问密码 |
| 设置 | 运行时目录 / Shim 目录、下载源（国内镜像 / 官方）、界面主题 |

## 🗂️ 数据存储

所有数据默认位于 `D:\环境管理\data`（D 盘不可用时回退到程序所在目录的 `data`）：

```
D:\环境管理\data\
├── box.json          # 配置：已安装/当前版本、目录、镜像偏好、主题
├── runtimes\<tool>\<ver>\   # 各版本安装目录
├── cache\            # 下载缓存 zip
├── shim\             # shim 转发脚本与 shim.cfg
├── mysql\<ver>\      # MySQL 实例配置（my.ini）
├── redis\<ver>\      # Redis 实例配置（redis.conf）
└── logs\
```

## 🔐 权限与安全

- 服务安装/启停、系统 PATH 写入需要管理员权限，程序启动即申请 UAC 提权。
- 密码等敏感信息在日志与界面中做掩码处理，绝不回显明文。
- 卸载来源为「系统」的版本时，仅移除 DevBox 登记，**绝不删除系统安装目录或系统自带服务**。

## 🏗️ 架构（`app/`）

| 模块 | 职责 |
|------|------|
| `config.py` | 配置层：`box.json` 加载/默认合并/原子保存、安装记录读写 |
| `providers.py` | 各环境版本源：列版本、构造下载 URL、镜像、解压后处理 |
| `downloader.py` | 下载与解压：分块下载、断点续传、多源回退、zip 校验、路径遍历防护 |
| `env.py` | 用户级环境变量同步（`JAVA_HOME` 等），失效项清理 |
| `shim.py` | 生成 shim 转发脚本与 `shim.cfg`，维护系统 PATH |
| `systemscan.py` | 只读扫描系统已安装环境 |
| `mysqlsvc.py` | MySQL 多实例服务管理（初始化/服务/密码/建用户/切换） |
| `redissvc.py` | Redis 多实例服务管理（服务/内存/密码/切换） |
| `uninstall.py` | 版本卸载与残留清理 |
| `gui.py` | tkinter 图形界面 |
| `devbox.py` | 程序入口：提权、引导、启动界面 |

## ❓ 常见问题

- **命令没切换到新版本？** 请关闭所有已打开的 cmd 窗口后重新打开（PATH 变更需新进程生效）。
- **服务相关操作失败？** 确认已以管理员身份运行（程序会自动请求提权）。
- **MySQL/Redis 配置里的中文路径乱码？** 配置文件（`my.ini` / `redis.conf` / `shim.cfg`）均按系统 ANSI(GBK) 编码写入，以匹配原生程序读取路径的方式。

## 📝 开源许可

本项目基于 [MIT License](./LICENSE) 开源。

© 2026 子柒
