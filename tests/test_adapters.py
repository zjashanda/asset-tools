# -*- coding: utf-8 -*-
"""adapters：BLOCKED 契约、local/http 行为、委托 CLI 协议。

全部测试在无网络、无凭据、无 sshpass 的机器上也能通过：需要网络的路径要么
走本机 127.0.0.1 的 http.server，要么断言"连不上 → EXIT_NETWORK"。
"""

import http.server
import os
import socket
import stat
import threading
import unittest

import ark_testlib as lib
from asset_tools import util
from asset_tools.adapters import ADAPTERS, ADAPTER_NAMES, get_adapter, probe_all
from asset_tools.adapters.base import AssetRef


def write_executable(path, content):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class RegistryTest(unittest.TestCase):
    def test_registry_names(self):
        self.assertEqual(
            set(ADAPTER_NAMES),
            {"local", "http", "oss", "node-storage", "email", "nas"},
        )
        self.assertEqual(set(ADAPTERS), set(ADAPTER_NAMES))

    def test_unknown_adapter_raises(self):
        with self.assertRaises(KeyError):
            get_adapter("ftp")

    def test_probe_all_never_raises(self):
        with lib.env_guard():
            statuses = probe_all()
        self.assertEqual(len(statuses), 6)
        for status in statuses:
            self.assertIsInstance(status.available, bool)
            self.assertTrue(status.reason)

    def test_probe_is_cached_and_safe(self):
        with lib.env_guard():
            local = get_adapter("local")
            first = local._probe()
            second = local._probe()
        self.assertEqual(first, second)


class OssTest(unittest.TestCase):
    def test_probe_blocked_without_env(self):
        with lib.env_guard():
            status = get_adapter("oss").probe()
        self.assertFalse(status.available)
        self.assertEqual(status.reason, "BLOCKED:ADAPTER_UNAVAILABLE")
        self.assertIn("ARK_OSS_ENDPOINT", status.missing_env)
        self.assertIn("ARK_OSS_ACCESS_KEY_SECRET", status.missing_env)

    def test_fetch_blocked_returns_not_ok(self):
        with lib.temp_dir() as tmp:
            with lib.env_guard():
                result = get_adapter("oss").fetch(
                    AssetRef(type="oss", filename="x.bin", ref="key/x.bin"), tmp
                )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "BLOCKED:ADAPTER_UNAVAILABLE")
        self.assertEqual(result.exit_code, util.EXIT_NO_SOURCE)

    def test_discover_blocked_returns_empty(self):
        with lib.env_guard():
            self.assertEqual(get_adapter("oss").discover({"key": "k"}), [])

    def test_probe_available_when_env_present(self):
        with lib.env_guard(
            ARK_OSS_ENDPOINT="https://oss.example.invalid",
            ARK_OSS_BUCKET="bucket-example",
            ARK_OSS_ACCESS_KEY_ID="id" + "example",
            ARK_OSS_ACCESS_KEY_SECRET="secret" + "example",
        ):
            status = get_adapter("oss").probe()
        self.assertTrue(status.available)
        self.assertEqual(status.reason, "OK")


class NodeStorageTest(unittest.TestCase):
    def test_probe_blocked_without_env(self):
        with lib.env_guard():
            status = get_adapter("node-storage").probe()
        self.assertFalse(status.available)
        self.assertEqual(status.reason, "BLOCKED:ADAPTER_UNAVAILABLE")
        self.assertIn("ARK_STORAGE_SSH_PASS", status.missing_env)

    def test_fetch_blocked(self):
        with lib.temp_dir() as tmp:
            with lib.env_guard():
                result = get_adapter("node-storage").fetch(
                    AssetRef(type="node-storage", filename="x.bin", ref="/srv/storage/ark/artifacts/x.bin"),
                    tmp,
                )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "BLOCKED:ADAPTER_UNAVAILABLE")

    def test_resolve_remote_accepts_under_base(self):
        adapter = get_adapter("node-storage")
        self.assertEqual(
            adapter.resolve_remote("/srv/storage/ark/artifacts/fw/x.zip"),
            "/srv/storage/ark/artifacts/fw/x.zip",
        )
        self.assertEqual(
            adapter.resolve_remote("fw/x.zip"), "/srv/storage/ark/artifacts/fw/x.zip"
        )

    def test_resolve_remote_rejects_escape(self):
        adapter = get_adapter("node-storage")
        for bad in ("../etc/passwd", "/etc/passwd", "/srv/storage/ark/artifacts/../secret"):
            with self.assertRaises(util.PathForbidden):
                adapter.resolve_remote(bad)


class DelegatedCliTest(unittest.TestCase):
    def test_email_probe_blocked_without_cli(self):
        with lib.env_guard():
            status = get_adapter("email").probe()
        self.assertFalse(status.available)
        self.assertEqual(status.reason, "BLOCKED:ADAPTER_UNAVAILABLE")

    def test_nas_probe_blocked_without_cli(self):
        with lib.env_guard():
            status = get_adapter("nas").probe()
        self.assertFalse(status.available)
        self.assertEqual(status.reason, "BLOCKED:ADAPTER_UNAVAILABLE")

    def test_email_discover_blocked_empty(self):
        with lib.env_guard():
            self.assertEqual(get_adapter("email").discover({"subject_contains": "fw"}), [])

    def test_email_delegated_roundtrip(self):
        with lib.temp_dir() as tmp:
            cli = os.path.join(tmp, "mail-cli")
            listing = (
                '[{"id":"m1","subject":"FW 2026","from":"a@example.invalid",'
                '"received_at":"2026-11-10 09:00:00","filename":"sample-firmware.txt","bytes":32}]'
            )
            script = "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -e",
                    "if [ \"$1\" = list ]; then",
                    "  echo '" + listing + "'",
                    "  exit 0",
                    "fi",
                    "out=\"$4\"",
                    "mkdir -p \"$out\"",
                    "cp \"" + lib.FIRMWARE_FIXTURE + "\" \"$out/sample-firmware.txt\"",
                    "echo \"{\\\"path\\\": \\\"$out/sample-firmware.txt\\\"}\"",
                    "",
                ]
            )
            write_executable(cli, script)
            with lib.env_guard(ASSET_EMAIL_CLI=cli):
                adapter = get_adapter("email")
                refs = adapter.discover({"subject_contains": "fw"})
                self.assertEqual(len(refs), 1)
                self.assertEqual(refs[0].filename, "sample-firmware.txt")
                result = adapter.fetch(refs[0], os.path.join(tmp, "dest"))
            self.assertTrue(result.ok, result.detail)
            self.assertEqual(result.sha256, lib.sha256_file(lib.FIRMWARE_FIXTURE))
            self.assertTrue(os.path.isfile(result.path))


class LocalAdapterTest(unittest.TestCase):
    def test_probe_ok(self):
        status = get_adapter("local").probe()
        self.assertTrue(status.available)
        self.assertEqual(status.reason, "OK")

    def test_fetch_verifies_sha(self):
        with lib.temp_dir() as tmp:
            dest = os.path.join(tmp, "dest")
            ref = AssetRef(
                type="local",
                filename="sample-firmware.txt",
                ref=lib.FIRMWARE_FIXTURE,
                sha256=lib.sha256_file(lib.FIRMWARE_FIXTURE),
            )
            result = get_adapter("local").fetch(ref, dest)
            self.assertTrue(result.ok, result.detail)
            self.assertEqual(util.sha256_file(result.path), ref.sha256)

    def test_fetch_sha_mismatch(self):
        with lib.temp_dir() as tmp:
            ref = AssetRef(
                type="local",
                filename="sample-firmware.txt",
                ref=lib.FIRMWARE_FIXTURE,
                sha256="d" * 64,
            )
            result = get_adapter("local").fetch(ref, tmp)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "SHA256_MISMATCH")
        self.assertEqual(result.exit_code, util.EXIT_FAIL)

    def test_fetch_missing_source(self):
        with lib.temp_dir() as tmp:
            ref = AssetRef(type="local", filename="nope.bin", ref=os.path.join(tmp, "nope.bin"))
            result = get_adapter("local").fetch(ref, tmp)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, util.EXIT_NO_SOURCE)

    def test_fetch_forbidden_outside_allowed_roots(self):
        with lib.temp_dir() as tmp:
            allowed = os.path.join(tmp, "allowed")
            os.makedirs(allowed)
            adapter = get_adapter("local", {"allowed_roots": [allowed]})
            ref = AssetRef(type="local", filename="sample.bin", ref=lib.FIRMWARE_FIXTURE)
            result = adapter.fetch(ref, os.path.join(tmp, "dest"))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "FORBIDDEN")
        self.assertEqual(result.exit_code, util.EXIT_FORBIDDEN)

    def test_discover_local_paths(self):
        refs = get_adapter("local").discover({"paths": [lib.FIRMWARE_FIXTURE]})
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].sha256, lib.sha256_file(lib.FIRMWARE_FIXTURE))


class _Handler(http.server.BaseHTTPRequestHandler):
    body = b""

    def log_message(self, *args):  # noqa: D401 - 静默测试服务器
        return

    def do_GET(self):  # noqa: N802 - http.server 约定
        if self.path == "/ok":
            payload = self.body
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/short":
            payload = self.body
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload) + 100))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404, "missing")


class HttpAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(lib.FIRMWARE_FIXTURE, "rb") as handle:
            cls.body = handle.read()
        handler = type("Handler", (_Handler,), {"body": cls.body})
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def base(self):
        return "http://127.0.0.1:{0}".format(self.port)

    def test_probe_ok(self):
        status = get_adapter("http").probe()
        self.assertTrue(status.available)

    def test_fetch_ok(self):
        with lib.temp_dir() as tmp:
            ref = AssetRef(
                type="http",
                filename="sample-firmware.txt",
                ref=self.base() + "/ok",
                sha256=lib.sha256_file(lib.FIRMWARE_FIXTURE),
            )
            result = get_adapter("http").fetch(ref, tmp)
            self.assertTrue(result.ok, result.detail)
            self.assertEqual(result.bytes, os.path.getsize(lib.FIRMWARE_FIXTURE))

    def test_fetch_length_mismatch_still_ok(self):
        with lib.temp_dir() as tmp:
            ref = AssetRef(
                type="http",
                filename="sample-firmware.txt",
                ref=self.base() + "/short",
                sha256=lib.sha256_file(lib.FIRMWARE_FIXTURE),
            )
            result = get_adapter("http").fetch(ref, tmp)
        self.assertTrue(result.ok, result.detail)
        self.assertIn("Content-Length", result.detail)

    def test_fetch_404_is_no_source(self):
        with lib.temp_dir() as tmp:
            ref = AssetRef(type="http", filename="x.bin", ref=self.base() + "/missing")
            result = get_adapter("http").fetch(ref, tmp)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "HTTP_NOT_FOUND")
        self.assertEqual(result.exit_code, util.EXIT_NO_SOURCE)

    def test_fetch_rejects_file_scheme(self):
        with lib.temp_dir() as tmp:
            ref = AssetRef(type="http", filename="x.bin", ref="file:///etc/passwd")
            result = get_adapter("http").fetch(ref, tmp)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "FORBIDDEN")
        self.assertEqual(result.exit_code, util.EXIT_FORBIDDEN)

    def test_connection_error_is_network(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with lib.temp_dir() as tmp:
            ref = AssetRef(type="http", filename="x.bin", ref="http://127.0.0.1:{0}/x".format(port))
            result = get_adapter("http").fetch(ref, tmp)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, util.EXIT_NETWORK)

    def test_fetch_sha_mismatch(self):
        with lib.temp_dir() as tmp:
            ref = AssetRef(
                type="http",
                filename="sample-firmware.txt",
                ref=self.base() + "/ok",
                sha256="e" * 64,
            )
            result = get_adapter("http").fetch(ref, tmp)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "SHA256_MISMATCH")

    def test_discover_filters_non_http(self):
        refs = get_adapter("http").discover({"urls": ["ftp://example/x", self.base() + "/ok"]})
        self.assertEqual(len(refs), 1)
        self.assertTrue(refs[0].ref.startswith("http://"))


if __name__ == "__main__":
    unittest.main()
