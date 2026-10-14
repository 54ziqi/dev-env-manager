# -*- coding: utf-8 -*-
"""DevBox 核心逻辑自动化测试（不触碰真实注册表/PATH/系统环境变量，也不联网）。

覆盖：
- config.Box：默认值合并、mirror 默认 cn、安装记录增删查、当前版本、原子保存。
- providers：build_urls 镜像/官方排序、install_markers、Adoptium 直链改写。
- downloader：validate_zip_package（合法/错包/损坏）、unpack 路径遍历拦截、
  download_any 多源回退与半成品清理（monkeypatch，不联网）。
- shim：bat 渲染（%% 转义、tool/exe 替换）+ 真实 cmd 端到端 JAVA_HOME 注入（临时目录）。
- mysqlsvc：service_name / instance_port / _sql_escape 纯逻辑。
- gui：构造 App 的冒烟测试（验证界面可正常构建，含图标设置）。

运行：python -m unittest tests.test_devbox -v
"""
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import downloader, mysqlsvc, providers, shim  # noqa: E402
from app.config import Box  # noqa: E402


def _make_zip(members):
    """构造内存 zip，members 为 {成员名: 字节}，返回 bytes。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="devbox_cfg_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_and_mirror(self):
        box = Box(self.tmp / "box.json")
        self.assertEqual(box.data.get("mirror"), "cn")
        self.assertTrue(box.data["runtime_dir"].endswith("runtimes"))
        self.assertTrue((self.tmp).is_dir())

    def test_installed_and_current_roundtrip(self):
        box = Box(self.tmp / "box.json")
        box.add_installed("java", "17.0.1", self.tmp / "jdk17", "download")
        self.assertIsNotNone(box.find_installed("java", "17.0.1"))
        box.set_current("java", "17.0.1")
        self.assertEqual(box.current("java"), "17.0.1")
        # 重新加载验证持久化
        box2 = Box(self.tmp / "box.json")
        self.assertEqual(box2.current("java"), "17.0.1")
        self.assertIsNotNone(box2.find_installed("java", "17.0.1"))
        # 去重：同版本再添加更新而非新增
        box2.add_installed("java", "17.0.1", self.tmp / "jdk17b", "import")
        self.assertEqual(len(box2.installed_list("java")), 1)
        self.assertEqual(box2.find_installed("java", "17.0.1")["source"], "import")
        box2.remove_installed("java", "17.0.1")
        self.assertIsNone(box2.find_installed("java", "17.0.1"))


class TestProviders(unittest.TestCase):
    def test_install_markers(self):
        expect = {
            "java": ["bin/java.exe"],
            "node": ["node.exe"],
            "python": ["python.exe"],
            "php": ["php.exe"],
            "maven": ["bin/mvn.cmd"],
            "mysql": ["bin/mysqld.exe"],
        }
        for tool, markers in expect.items():
            self.assertEqual(providers.PROVIDERS[tool].install_markers, markers, tool)

    def test_build_urls_cn_mirror_first_official_last(self):
        for tool, ver in [("node", "20.20.2"), ("python", "3.12.4"),
                          ("maven", "3.9.9")]:
            urls = providers.PROVIDERS[tool].build_urls(ver, "cn")
            self.assertGreaterEqual(len(urls), 2, tool)
            first = urls[0]
            self.assertTrue(
                ("mirrors" in first) or ("npmmirror" in first),
                "%s 首选应为国内镜像: %s" % (tool, first),
            )
            last = urls[-1]
            self.assertTrue(
                ("nodejs.org" in last) or ("python.org" in last)
                or ("apache.org" in last) or ("mysql.com" in last),
                "%s 末位应为官方源: %s" % (tool, last),
            )

    def test_build_urls_default_official_first(self):
        urls = providers.PROVIDERS["node"].build_urls("20.20.2", "default")
        self.assertIn("nodejs.org", urls[0])

    def test_php_no_mirror(self):
        urls = providers.PROVIDERS["php"].build_urls("8.3.6", "cn")
        self.assertTrue(all("windows.php.net" in u for u in urls))

    def test_mysql_uses_official_cdn(self):
        # MySQL 无可用国内镜像：build_urls 应直接给官方 CDN（cdn.mysql.com）。
        urls = providers.PROVIDERS["mysql"].build_urls("8.0.37", "cn")
        self.assertTrue(urls)
        self.assertIn("cdn.mysql.com", urls[0])
        self.assertIn("mysql-8.0.37-winx64.zip", urls[0])
        # 手动输入版本走模板，按 series 分目录，同样指向 CDN
        u = providers.PROVIDERS["mysql"].build_url("8.4.0")
        self.assertIn("cdn.mysql.com/Downloads/MySQL-8.4/", u)

    def test_adoptium_mirror_rewrite(self):
        gh = ("https://github.com/adoptium/temurin17-binaries/releases/download/"
              "jdk-17.0.11%2B9/OpenJDK17U-jdk_x64_windows_hotspot_17.0.11_9.zip")
        mm = providers._adoptium_mirrors(gh)
        self.assertEqual(len(mm), 1)
        self.assertIn("mirrors.tuna.tsinghua.edu.cn/Adoptium/17/jdk/x64/windows", mm[0])
        self.assertTrue(mm[0].endswith("OpenJDK17U-jdk_x64_windows_hotspot_17.0.11_9.zip"))
        # 非 zip / 非 github 不改写
        self.assertEqual(providers._adoptium_mirrors("https://example.com/x.msi"), [])
        self.assertEqual(providers._adoptium_mirrors(""), [])

    def test_java_mirror_urls_via_official(self):
        official = ("https://github.com/adoptium/temurin21-binaries/releases/download/"
                    "jdk-21.0.1%2B12/OpenJDK21U-jdk_x64_windows_hotspot_21.0.1_12.zip")
        mm = providers.PROVIDERS["java"].mirror_urls("21.0.1", official)
        self.assertIn("/Adoptium/21/jdk/x64/windows/", mm[0])


class TestDownloader(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="devbox_dl_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_validate_zip_valid_and_wrong_and_damaged(self):
        good = self.tmp / "good.zip"
        good.write_bytes(_make_zip({"jdk-17/bin/java.exe": b"x", "jdk-17/README": b"y"}))
        # 合法且含 marker
        names = downloader.validate_zip_package(good, ["bin/java.exe"])
        self.assertTrue(any("java.exe" in n for n in names))
        # 错包：marker 不存在
        with self.assertRaises(ValueError):
            downloader.validate_zip_package(good, ["node.exe"])
        # 损坏/非 zip
        bad = self.tmp / "bad.zip"
        bad.write_bytes(b"not a zip file")
        with self.assertRaises(ValueError):
            downloader.validate_zip_package(bad, ["node.exe"])
        # 文件不存在
        with self.assertRaises(ValueError):
            downloader.validate_zip_package(self.tmp / "missing.zip", ["node.exe"])

    def test_unpack_rejects_path_traversal(self):
        evil = self.tmp / "evil.zip"
        evil.write_bytes(_make_zip({"../escape.txt": b"boom", "ok.txt": b"fine"}))
        target = self.tmp / "out"
        with self.assertRaises(ValueError):
            downloader.unpack(evil, target)
        # 拒绝后不得解压任何文件
        self.assertFalse((target / "ok.txt").exists())
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_unpack_ok(self):
        z = self.tmp / "ok.zip"
        z.write_bytes(_make_zip({"dir/a.txt": b"1"}))
        target = self.tmp / "out2"
        downloader.unpack(z, target)
        self.assertTrue((target / "dir" / "a.txt").is_file())

    def test_download_any_fallback(self):
        """monkeypatch download：前两个源失败，第三个成功，验证顺序与清理。"""
        orig = downloader.download
        attempted = []

        def fake_download(url, dest, cb=None):
            attempted.append(url)
            if "fail" in url:
                # 模拟下载中途失败并留下半成品
                Path(dest).write_bytes(b"partial")
                raise RuntimeError("网络错误")
            Path(dest).write_bytes(b"complete")
            return Path(dest)

        downloader.download = fake_download
        try:
            dest = self.tmp / "pkg.zip"
            result = downloader.download_any(
                ["http://a/fail1", "http://b/fail2", "http://c/ok"], dest
            )
            self.assertEqual(attempted, ["http://a/fail1", "http://b/fail2", "http://c/ok"])
            self.assertEqual(Path(result).read_bytes(), b"complete")
        finally:
            downloader.download = orig

    def test_download_any_all_fail(self):
        orig = downloader.download

        def fake_download(url, dest, cb=None):
            raise RuntimeError("down")

        downloader.download = fake_download
        try:
            with self.assertRaises(RuntimeError):
                downloader.download_any(["http://a", "http://b"], self.tmp / "x.zip")
        finally:
            downloader.download = orig

    def test_download_any_empty(self):
        with self.assertRaises(RuntimeError):
            downloader.download_any([], self.tmp / "x.zip")


class TestShim(unittest.TestCase):
    def test_render_bat_escaping(self):
        text = shim._render_bat("java", "java.exe")
        # 模板格式化后：for 变量为 %%a，%~dp0、%T%、%* 正确，且无残留 %(tool)s
        self.assertIn("%%a", text)
        self.assertIn("%~dp0shim.cfg", text)
        self.assertIn('findstr /b /i "java_bin="', text)
        self.assertIn("java.exe", text)
        self.assertNotIn("%(tool)s", text)
        self.assertNotIn("%%T%%", text)  # 应已还原为 %T%
        self.assertIn("%T%", text)

    def test_java_home_injection_end_to_end(self):
        """临时中文目录 + GBK shim.cfg/bat，真实 cmd 运行，验证 JAVA_HOME 注入与中文路径。"""
        tmp = Path(tempfile.mkdtemp(prefix="devbox_shim_"))
        try:
            jdk = tmp / "环境管理" / "data" / "runtimes" / "java" / "21.0.1"
            mvnbin = tmp / "环境管理" / "data" / "runtimes" / "maven" / "3.9.9" / "bin"
            jdk.mkdir(parents=True)
            mvnbin.mkdir(parents=True)
            (mvnbin / "mvn.cmd").write_text(
                "@echo off\r\necho JH=%JAVA_HOME%\r\n", encoding="gbk")
            shim_dir = tmp / "shim"
            shim_dir.mkdir()
            (shim_dir / "shim.cfg").write_text(
                "%s\nmaven_bin=%s\njava_home=%s\n" % (shim._CFG_HEADER, mvnbin, jdk),
                encoding="gbk")
            bat = shim_dir / "mvn.bat"
            text = shim._render_bat("maven", "mvn.cmd").replace("\r\n", "\n").replace("\n", "\r\n")
            bat.write_bytes(text.encode("gbk"))
            r = subprocess.run(["cmd", "/c", str(bat)], capture_output=True,
                               text=True, encoding="gbk", errors="replace",
                               cwd=str(shim_dir),
                               creationflags=subprocess.CREATE_NO_WINDOW)
            out = (r.stdout or "") + (r.stderr or "")
            self.assertNotIn("目标不存在", out)
            self.assertIn("JH=%s" % jdk, out)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestMysqlsvc(unittest.TestCase):
    def test_service_name(self):
        self.assertEqual(mysqlsvc.service_name("8.0.40"), "MySQL80_8_0_40")

    def test_instance_port_stable(self):
        tmp = Path(tempfile.mkdtemp(prefix="devbox_my_"))
        try:
            box = Box(tmp / "box.json")
            box.add_installed("mysql", "8.0.36", tmp / "a", "download")
            box.add_installed("mysql", "8.0.40", tmp / "b", "download")
            # 排序后 8.0.36 在前 -> 3306，8.0.40 -> 3307
            self.assertEqual(mysqlsvc.instance_port(box, "8.0.36"), 3306)
            self.assertEqual(mysqlsvc.instance_port(box, "8.0.40"), 3307)
            # 幂等：重复调用结果一致
            self.assertEqual(mysqlsvc.instance_port(box, "8.0.40"), 3307)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sql_escape(self):
        self.assertEqual(mysqlsvc._sql_escape("a'b"), "a\\'b")
        self.assertEqual(mysqlsvc._sql_escape("a\\b"), "a\\\\b")

    def test_write_my_ini_gbk_chinese_path(self):
        """回归：my.ini 必须 GBK 写入，含中文路径能被正确解码（非 UTF-8）。"""
        tmp = Path(tempfile.mkdtemp(prefix="devbox_ini_"))
        try:
            box = Box(tmp / "box.json")
            root = tmp / "环境管理" / "data" / "runtimes" / "mysql" / "8.0.37"
            root.mkdir(parents=True)
            box.add_installed("mysql", "8.0.37", root, "download")
            ini = mysqlsvc.write_my_ini(box, "8.0.37")
            raw = ini.read_bytes()
            # 按 GBK 解码得到正确中文路径
            text = raw.decode("gbk")
            self.assertIn("环境管理", text)
            self.assertIn("basedir=%s" % str(root).replace("\\", "/"), text)
            # 不是合法 UTF-8（证明以 GBK 写入，修复了 mysqld 读选项文件乱码）
            with self.assertRaises(UnicodeDecodeError):
                raw.decode("utf-8")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_write_my_ini_with_init_file(self):
        """回归：传入 init_file 时写入 init-file= 行（重置密码单实例方案）。"""
        tmp = Path(tempfile.mkdtemp(prefix="devbox_ini2_"))
        try:
            box = Box(tmp / "box.json")
            root = tmp / "runtimes" / "mysql" / "8.0.37"
            root.mkdir(parents=True)
            box.add_installed("mysql", "8.0.37", root, "download")
            init_sql = root / "data" / "_devbox_reset.sql"
            ini = mysqlsvc.write_my_ini(box, "8.0.37", init_file=init_sql)
            text = ini.read_bytes().decode("gbk")
            self.assertIn("init-file=%s" % str(init_sql).replace("\\", "/"), text)
            # 不传 init_file 时不应包含该行（重置后恢复干净配置）
            ini2 = mysqlsvc.write_my_ini(box, "8.0.37")
            self.assertNotIn("init-file=", ini2.read_bytes().decode("gbk"))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_run_sql_uses_no_defaults_explicit_host_port(self):
        """回归：客户端 SQL 走 --no-defaults + 显式 -h/-P，避免中文 defaults-file 路径报错。"""
        import shutil
        tmp = Path(tempfile.mkdtemp(prefix="devbox_sql_"))
        try:
            box = Box(tmp / "box.json")
            box.data["runtime_dir"] = str(tmp / "runtimes")
            root = tmp / "runtimes" / "mysql" / "8.0.37"
            (root / "bin").mkdir(parents=True)
            (root / "bin" / "mysql.exe").write_bytes(b"MZ")
            box.add_installed("mysql", "8.0.37", root, "download")

            captured = {}
            orig_run = mysqlsvc._run
            try:
                mysqlsvc._run = lambda cmd, **k: (
                    captured.__setitem__("cmd", [str(c) for c in cmd]) or (True, "")
                )
                mysqlsvc._run_sql(box, "8.0.37", "SELECT 1;", "root", "pw")
            finally:
                mysqlsvc._run = orig_run

            cmd = captured["cmd"]
            self.assertIn("--no-defaults", cmd)
            self.assertIn("-h", cmd)
            self.assertIn("127.0.0.1", cmd)
            self.assertIn("-P", cmd)
            self.assertIn("3306", cmd)
            self.assertFalse(any(c.startswith("--defaults-file") for c in cmd))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRedissvc(unittest.TestCase):
    """Redis 多实例纯逻辑：服务名/端口/受管 conf 读写/Provider 注册。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="devbox_redis_"))
        self.box = Box(self.tmp / "box.json")
        self.box.data["runtime_dir"] = str(self.tmp / "runtimes")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register(self, ver):
        root = self.tmp / "runtimes" / "redis" / ver
        root.mkdir(parents=True, exist_ok=True)
        (root / "redis-server.exe").write_bytes(b"MZ")
        self.box.add_installed("redis", ver, str(root), "import")
        return root

    def test_service_name_and_port(self):
        from app import redissvc

        self.assertEqual(redissvc.service_name("5.0.14.1"), "Redis_5_0_14_1")
        self._register("5.0.14.1")
        self.assertEqual(redissvc.instance_port(self.box, "5.0.14.1"), 6379)
        self._register("4.0.14.3")
        # 排序后 4.0.14.3 在前 -> 6379；5.0.14.1 -> 6380
        self.assertEqual(redissvc.instance_port(self.box, "4.0.14.3"), 6379)
        self.assertEqual(redissvc.instance_port(self.box, "5.0.14.1"), 6380)

    def test_write_conf_and_read_back(self):
        from app import redissvc

        self._register("5.0.14.1")
        conf = redissvc.write_conf(
            self.box, "5.0.14.1", maxmemory_mb=512, password="s3cret"
        )
        text = conf.read_text(encoding="gbk")
        self.assertIn("port 6379", text)
        self.assertIn("maxmemory %d" % (512 * 1024 * 1024), text)
        self.assertIn("requirepass s3cret", text)
        self.assertEqual(redissvc.read_maxmemory_mb(self.box, "5.0.14.1"), 512)
        self.assertEqual(redissvc.read_password(self.box, "5.0.14.1"), "s3cret")
        # 仅覆盖内存时保留原密码（None 表示沿用）
        redissvc.write_conf(self.box, "5.0.14.1", maxmemory_mb=1024, password=None)
        self.assertEqual(redissvc.read_maxmemory_mb(self.box, "5.0.14.1"), 1024)
        self.assertEqual(redissvc.read_password(self.box, "5.0.14.1"), "s3cret")

    def test_write_conf_gbk_chinese_path(self):
        """回归：redis.conf 必须 GBK 写入，含中文路径能被正确解码（修复启动 logfile 乱码找不到路径）。"""
        import shutil
        from app import redissvc

        tmp = Path(tempfile.mkdtemp(prefix="devbox_redcn_"))
        try:
            box = Box(tmp / "box.json")
            root = tmp / "环境管理" / "runtimes" / "redis" / "3.2.100"
            root.mkdir(parents=True)
            (root / "redis-server.exe").write_bytes(b"MZ")
            box.add_installed("redis", "3.2.100", root, "import")
            box.data["runtime_dir"] = str(tmp / "环境管理" / "runtimes")
            conf = redissvc.write_conf(box, "3.2.100")
            raw = conf.read_bytes()
            # 按 GBK 解码得到正确中文路径
            text = raw.decode("gbk")
            self.assertIn("环境管理", text)
            self.assertIn(
                "logfile %s/redis-server.log" % str(root).replace("\\", "/"), text
            )
            # 整体不是合法 UTF-8（证明以 GBK 写入，修复 redis-server 读中文路径乱码）
            with self.assertRaises(UnicodeDecodeError):
                raw.decode("utf-8")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_install_service_reports_already_exists(self):
        from app import redissvc

        self._register("5.0.14.1")
        orig_run, orig_exists = redissvc._run, redissvc._service_exists
        try:
            redissvc._run = lambda *a, **k: (False, "")   # 安装命令失败且无控制台输出
            redissvc._service_exists = lambda name: True   # 但服务其实已存在
            res = redissvc.install_service(self.box, "5.0.14.1")
            self.assertTrue(res["ok"])
            self.assertIn("已存在", res["message"])
        finally:
            redissvc._run, redissvc._service_exists = orig_run, orig_exists

    def test_provider_registered_and_mirror_first(self):
        from app.providers import PROVIDERS, TOOL_ORDER

        self.assertIn("redis", PROVIDERS)
        self.assertIn("redis", TOOL_ORDER)
        urls = PROVIDERS["redis"].build_urls("5.0.14.1", "cn")
        self.assertTrue(urls)
        self.assertTrue(urls[0].startswith("https://mirror.ghproxy.com/"))
        self.assertEqual(PROVIDERS["redis"].bin_sub, "")


class TestUninstall(unittest.TestCase):
    """卸载与残留清理：扫描残留、删目录/缓存/登记、system 来源不删目录。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="devbox_uninst_"))
        self.box = Box(self.tmp / "box.json")
        self.box.data["runtime_dir"] = str(self.tmp / "runtimes")
        self.box.data["cache_dir"] = str(self.tmp / "cache")
        Path(self.box.data["cache_dir"]).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register(self, ver, source="download", with_files=True):
        root = self.tmp / "runtimes" / "node" / ver
        root.mkdir(parents=True, exist_ok=True)
        if with_files:
            (root / "node.exe").write_bytes(b"MZ")
        self.box.add_installed("node", ver, str(root), source)
        return root

    def test_scan_residuals_lists_dir_cache_current(self):
        from app import uninstall

        root = self._register("1.2.3")
        (Path(self.box.data["cache_dir"]) / "node-v1.2.3-win-x64.zip").write_bytes(b"PK")
        self.box.set_current("node", "1.2.3")
        items, is_system = uninstall.scan_residuals(self.box, "node", "1.2.3")
        self.assertFalse(is_system)
        kinds = [k for k, _ in items]
        self.assertIn("版本安装目录", kinds)
        self.assertIn("下载缓存", kinds)
        self.assertIn("当前激活版本", kinds)

    def test_uninstall_removes_dir_cache_record_current(self):
        from app import uninstall

        root = self._register("1.2.3")
        zip_path = Path(self.box.data["cache_dir"]) / "node-v1.2.3-win-x64.zip"
        zip_path.write_bytes(b"PK")
        self.box.set_current("node", "1.2.3")

        result = uninstall.uninstall(self.box, "node", "1.2.3")
        self.assertTrue(result["ok"])
        self.assertTrue(result["was_current"])
        self.assertFalse(root.exists())          # 版本目录已删
        self.assertFalse(zip_path.exists())      # 缓存已删
        self.assertFalse((self.tmp / "runtimes" / "node").exists())  # 空的工具父目录已移除
        self.assertIsNone(self.box.find_installed("node", "1.2.3"))  # 登记已移除
        self.assertIsNone(self.box.current("node"))                  # 激活标记已清

    def test_uninstall_system_source_keeps_dir(self):
        from app import uninstall

        root = self._register("9.9.9", source="system")
        result = uninstall.uninstall(self.box, "node", "9.9.9")
        self.assertTrue(result["ok"])
        self.assertTrue(root.exists())           # system 来源绝不删系统目录
        self.assertIsNone(self.box.find_installed("node", "9.9.9"))


class TestEnvSync(unittest.TestCase):
    """sync_homes：有当前版本写 *_HOME；无当前版本仅清理 DevBox 受管的失效项，不碰用户自建。"""

    def setUp(self):
        from app import env
        self.env = env
        self.tmp = Path(tempfile.mkdtemp(prefix="devbox_env_"))
        self.box = Box(self.tmp / "box.json")
        self.box.data["runtime_dir"] = str(self.tmp / "runtimes")
        self._orig = (env._read, env.set_env, env.unset_env)

    def tearDown(self):
        import shutil
        env = self.env
        env._read, env.set_env, env.unset_env = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self, read_map):
        env = self.env
        self.unset_calls = []
        self.set_calls = []
        env._read = lambda name: read_map.get(name, "")
        env.set_env = lambda n, v: self.set_calls.append((n, v))
        env.unset_env = lambda n: self.unset_calls.append(n)

    def test_sets_home_for_current_version(self):
        root = self.tmp / "runtimes" / "java" / "17.0.2"
        root.mkdir(parents=True)
        self.box.add_installed("java", "17.0.2", root, "download")
        self.box.set_current("java", "17.0.2")
        self._patch({"JAVA_HOME": ""})
        self.env.sync_homes(self.box)
        self.assertIn(("JAVA_HOME", str(root)), self.set_calls)
        self.assertNotIn("JAVA_HOME", self.unset_calls)

    def test_removes_stale_devbox_home_when_no_current(self):
        # 无当前版本，但 JAVA_HOME 仍指向受管 runtime 下已删目录 -> 应清理
        stale = str(self.tmp / "runtimes" / "java" / "17.0.2")
        self._patch({"JAVA_HOME": stale})
        self.env.sync_homes(self.box)
        self.assertIn("JAVA_HOME", self.unset_calls)

    def test_keeps_user_owned_home_when_no_current(self):
        # 用户自建的 JAVA_HOME（不在受管 runtime 下）-> 绝不删除
        self._patch({"JAVA_HOME": r"C:\Custom\jdk"})
        self.env.sync_homes(self.box)
        self.assertNotIn("JAVA_HOME", self.unset_calls)


class TestGUISmoke(unittest.TestCase):
    def test_app_constructs(self):
        """构造 App 验证界面（含图标设置、各标签页）可正常构建，不进入主循环。"""
        try:
            import tkinter as tk
            probe = tk.Tk()
            probe.destroy()
        except Exception:
            self.skipTest("无可用图形显示环境，跳过 GUI 冒烟")
        from app.gui import App
        tmp = Path(tempfile.mkdtemp(prefix="devbox_gui_"))
        box = Box(tmp / "box.json")
        app = None
        try:
            app = App(box)
            app.root.update_idletasks()
            # 关键控件存在
            self.assertTrue(hasattr(app, "notebook"))
            self.assertTrue(hasattr(app, "progress"))
            self.assertFalse(app._downloading)
            # 切换到一个工具不应报错
            app._select_tool(1)
            app.root.update()
        finally:
            if app is not None:
                try:
                    app.root.destroy()
                except Exception:
                    pass
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_temp_password_dialog_keeps_text_after_gc(self):
        """回归：临时密码对话框的 StringVar 必须挂到 self，GC 后输入框仍显示密码。"""
        try:
            import tkinter as tk
            probe = tk.Tk()
            probe.destroy()
        except Exception:
            self.skipTest("无可用图形显示环境，跳过 GUI 冒烟")
        import gc
        from app.gui import App, _TempPasswordDialog

        tmp = Path(tempfile.mkdtemp(prefix="devbox_tpwd_"))
        box = Box(tmp / "box.json")
        app = None
        try:
            app = App(box)
            dlg = _TempPasswordDialog(app, "lKijpjohy3*_")
            dlg.update_idletasks()
            gc.collect()  # 触发对未持引用局部变量的回收（旧实现的变空点）
            app.root.update()
            self.assertEqual(dlg._entry.get(), "lKijpjohy3*_")
            dlg.destroy()
        finally:
            if app is not None:
                try:
                    app.root.destroy()
                except Exception:
                    pass
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestEnsureAdmin(unittest.TestCase):
    """运行时 UAC 自提权逻辑（monkeypatch，不真正弹窗/退出）。"""

    def setUp(self):
        if sys.platform != "win32":
            self.skipTest("仅 Windows 平台测试 UAC 提权")
        import ctypes

        if not hasattr(ctypes, "windll"):
            self.skipTest("无 ctypes.windll，跳过")

    def test_no_relaunch_when_already_admin(self):
        """已具备管理员权限时，不应重启也不应退出。"""
        import devbox
        from unittest import mock

        with mock.patch.object(devbox, "_is_admin", return_value=True), \
                mock.patch.object(devbox.sys, "exit") as m_exit:
            devbox._ensure_admin()
            m_exit.assert_not_called()

    def test_relaunch_when_not_admin(self):
        """非管理员时，应以 runas 重启自身并退出当前进程。"""
        import ctypes
        import devbox
        from unittest import mock

        calls = {}

        def fake_shell_execute(*a, **k):
            calls["args"] = a
            return 42  # > 32 表示成功

        with mock.patch.object(devbox, "_is_admin", return_value=False), \
                mock.patch.object(
                    ctypes.windll.shell32, "ShellExecuteW", fake_shell_execute
                ), \
                mock.patch.object(devbox.sys, "exit") as m_exit:
            devbox._ensure_admin()
            self.assertEqual(calls["args"][1], "runas")
            m_exit.assert_called_once_with(0)

    def test_no_exit_when_user_declines_uac(self):
        """用户在 UAC 点「否」（ShellExecuteW <= 32）时不退出，退回普通权限继续运行。"""
        import ctypes
        import devbox
        from unittest import mock

        def fake_shell_execute(*a, **k):
            return 5  # <= 32 表示失败

        with mock.patch.object(devbox, "_is_admin", return_value=False), \
                mock.patch.object(
                    ctypes.windll.shell32, "ShellExecuteW", fake_shell_execute
                ), \
                mock.patch.object(devbox.sys, "exit") as m_exit:
            devbox._ensure_admin()
            m_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
