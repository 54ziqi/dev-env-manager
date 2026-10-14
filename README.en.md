# DevBox — Windows Multi-Version Development Environment Manager

> A graphical tool to download, install and switch **Java / Node.js / Python / PHP / Maven / MySQL / Redis** on Windows, with multi-instance service management for MySQL and Redis.

- **Author**: 子柒 (Ziqi)
- **License**: [Non-Commercial Use License](./LICENSE)
- **Platform**: Windows 10 / 11 (x64)
- **Version**: 1.0.0
- **Tech stack**: Python 3 (standard library + tkinter only) · PyInstaller · NSIS · Authenticode signing

🇨 中文文档：[README.md](./README.md)

---

## 📥 Downloads

Grab the latest build from the [`release/`](./release/) folder:

- **Installer**: `DevBox-Setup-<version>.exe`
- **Portable**: `DevBox-<version>.zip` (unzip and double-click `DevBox.exe`)

See [release/CHANGELOG.md](./release/CHANGELOG.md) for version history. All artifacts are Authenticode-signed (publisher CN=子柒).

## ✨ Highlights

- **Coexisting versions with instant switching** — each version is installed independently; a `shim` forwarding script + `shim.cfg` act as a transparent proxy for global commands. Switching a version only rewrites config — no reinstall, no PATH edits.
- **Mirror-first downloads** — multiple candidate sources (domestic mirrors first, official fallback), resumable downloads with automatic retries, and post-download archive validation (zip integrity + required files) to prevent wrong/corrupted packages.
- **MySQL service management** — per-version isolated instances (own port / service name / data dir / config dir): initialize instance, install/start/stop/uninstall the Windows service, change & reset the root password, create users, restart-on-switch, and extract & copy the initial temporary password from the error log.
- **Redis service management** — per-version instances with install/start/stop/uninstall, memory limit (`maxmemory`), access password (`requirepass`), and version switching.
- **Thorough uninstall + residual cleanup** — removing a version cleans up Windows services, leftover processes, install/instance directories, download cache, the active flag and stale user environment variables (`*_HOME`); a residual list is shown before uninstalling.
- **Runtime administrator privileges** — requests UAC elevation on startup (manifest `requireAdministrator` plus runtime self-elevation) so service operations and system PATH writes work.
- **Keeps the C: drive clean** — all downloads, extracted versions, configs and logs live under `D:\环境管理\data`.
- **Chinese-friendly GUI** — built on tkinter with light/dark themes, a custom window icon, and password visibility toggles.

## 🧰 Supported Environments

| Environment | Source | Service management |
|------|------|:--:|
| Java | Eclipse Temurin (Adoptium) | — |
| Node.js | nodejs.org official builds | — |
| Python | Windows embeddable packages | — |
| PHP | Windows NTS builds | — |
| Maven | Apache official archive | — |
| MySQL | Community winx64 zip | ✅ multi-instance service + init + password |
| Redis | Windows builds (tporadowski / MicrosoftArchive) | ✅ multi-instance service + memory + password |

## 🚀 Getting Started

### As an end user

1. Download and run `DevBox-Setup.exe`, choose an install location (a non-system drive such as `D:\DevBox` is recommended).
2. Allow the UAC prompt on first launch.
3. Pick an environment on the left → "Download" tab to refresh & download → "Installed" tab, then "Set as current".
4. Open a new terminal; `java -version` / `node -v` / `mvn -v` now point to the selected version.

### Run from source

```bash
# Requires Python 3.8+ with tkinter (bundled with the official Windows installer)
python devbox.py
```

> Running needs no third-party packages (standard library only); `pyinstaller` in `requirements.txt` is only for packaging.

## 🛠️ Build & Test

```bash
pip install -r requirements.txt   # only needed for packaging (PyInstaller)

python build.py --clean           # full pipeline: PyInstaller -> sign -> zip -> NSIS installer -> sign
python build.py --no-nsis         # skip the NSIS installer
python build.py --nsis-only       # regenerate installer only (reuse existing dist)

python -m unittest tests.test_devbox   # run unit tests
```

Artifacts are produced under `dist/`: `DevBox/` (portable folder), `DevBox.zip`, and `DevBox-Setup.exe` (Authenticode-signed, publisher CN=子柒).

## 🖥️ Interface

| Tab | Purpose |
|-----|---------|
| Installed | List installed versions; "Set as current" / quick-switch dropdown / double-click; import local zip; scan system; uninstall (with residual cleanup) |
| Download | Refresh online version list, download and install |
| MySQL Service | Initialize instance, install/start/stop/uninstall service, change/reset root password, create user, view temporary password |
| Redis Service | Install/start/stop/uninstall service, set memory limit, set access password |
| Settings | Runtime / shim directories, download source (mirror / official), UI theme |

## 🗂️ Data Location

All data defaults to `D:\环境管理\data` (falls back to a `data` folder next to the program if the D: drive is unavailable):

```
D:\环境管理\data\
├── box.json                    # config: installed/current versions, dirs, mirror, theme
├── runtimes\<tool>\<ver>\      # per-version install directories
├── cache\                      # downloaded zip cache
├── shim\                       # shim scripts and shim.cfg
├── mysql\<ver>\                # MySQL instance config (my.ini)
├── redis\<ver>\                # Redis instance config (redis.conf)
└── logs\
```

## 🔐 Permissions & Security

- Service install/start/stop and system PATH writes require administrator rights; the app requests UAC elevation on startup.
- Sensitive values (passwords) are masked in logs and the UI and never shown in plaintext.
- Uninstalling a version whose source is "system" only removes the DevBox record — it **never deletes the system install directory or system services**.

## 🏗️ Architecture (`app/`)

| Module | Responsibility |
|--------|----------------|
| `config.py` | Config layer: load/merge/atomically save `box.json`, read/write install records |
| `providers.py` | Per-environment version sources: list versions, build URLs, mirrors, post-install |
| `downloader.py` | Download & extract: chunked/resumable download, multi-source fallback, zip validation, path-traversal guard |
| `env.py` | User environment-variable sync (`JAVA_HOME`, etc.) and stale-entry cleanup |
| `shim.py` | Generate shim scripts and `shim.cfg`, maintain system PATH |
| `systemscan.py` | Read-only scan of system-installed environments |
| `mysqlsvc.py` | MySQL multi-instance service management (init/service/password/user/switch) |
| `redissvc.py` | Redis multi-instance service management (service/memory/password/switch) |
| `uninstall.py` | Version uninstall and residual cleanup |
| `gui.py` | tkinter graphical interface |
| `devbox.py` | Entry point: elevation, bootstrap, launch UI |

## ❓ FAQ

- **Commands didn't switch to the new version?** Close all open cmd windows and open a new one (PATH changes apply to new processes).
- **Service operations fail?** Make sure the app runs as administrator (it requests elevation automatically).
- **Garbled Chinese paths in MySQL/Redis config?** Config files (`my.ini` / `redis.conf` / `shim.cfg`) are written using the system ANSI code page (GBK) to match how the native programs read paths.

## 📝 License

This project is licensed under the [Non-Commercial Use License](./LICENSE): free for research and personal use, commercial use prohibited. Contact the copyright holder for a commercial license.

© 2026 子柒 (Ziqi)
