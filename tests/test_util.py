# -*- coding: utf-8 -*-
"""util：时间契约、退出码、哈希、脱敏、原子写。"""

import hashlib
import os
import re
import unittest
from datetime import datetime, timedelta, timezone

import ark_testlib as lib
from asset_tools import util


class TimeContractTest(unittest.TestCase):
    def test_now_beijing_offset(self):
        now = util.now_beijing()
        self.assertEqual(now.utcoffset(), timedelta(hours=8))

    def test_format_display_shape(self):
        text = util.format_display()
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertNotIn("T", text)
        self.assertNotIn("+08", text)

    def test_format_rfc3339_shape(self):
        text = util.format_rfc3339()
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+0800$")

    def test_format_stamp_shape(self):
        self.assertRegex(util.format_stamp(), r"^\d{8}-\d{6}$")

    def test_format_uses_given_datetime(self):
        moment = datetime(2026, 11, 10, 19, 30, 0, tzinfo=util.BEIJING)
        self.assertEqual(util.format_display(moment), "2026-11-10 19:30:00")
        self.assertEqual(util.format_rfc3339(moment), "2026-11-10T19:30:00+0800")
        self.assertEqual(util.format_stamp(moment), "20261110-193000")

    def test_display_time_roundtrip(self):
        moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=util.BEIJING)
        parsed = util.parse_time(util.format_display(moment))
        self.assertEqual(parsed, moment)
        self.assertEqual(parsed.tzinfo.utcoffset(parsed), timedelta(hours=8))

    def test_parse_rfc3339_and_z(self):
        first = util.parse_time("2026-11-10T11:30:00+0800")
        second = util.parse_time("2026-11-10T03:30:00Z")
        self.assertEqual(first, second)
        self.assertEqual(first.utcoffset(), timedelta(hours=8))

    def test_parse_time_rejects_garbage(self):
        with self.assertRaises(ValueError):
            util.parse_time("2026/11/10 11:30")

    def test_exit_code_table(self):
        self.assertEqual(util.EXIT_CODES[0], "OK")
        self.assertEqual(util.EXIT_CODES[1], "FAIL")
        self.assertEqual(util.EXIT_CODES[2], "USAGE")
        self.assertEqual(util.EXIT_CODES[3], "NO_SOURCE")
        self.assertEqual(util.EXIT_CODES[4], "FORBIDDEN")
        self.assertEqual(util.EXIT_CODES[5], "NETWORK")
        self.assertEqual(util.EXIT_CODES[6], "NEEDS_ELEVATION")
        self.assertEqual(util.exit_name(3), "NO_SOURCE")


class HashTest(unittest.TestCase):
    def test_sha256_file_matches_hashlib(self):
        with open(lib.FIRMWARE_FIXTURE, "rb") as handle:
            expected = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(util.sha256_file(lib.FIRMWARE_FIXTURE), expected)
        self.assertRegex(expected, r"^[a-f0-9]{64}$")

    def test_sha256_bytes(self):
        self.assertEqual(util.sha256_bytes(b"ark"), hashlib.sha256(b"ark").hexdigest())

    def test_human_bytes(self):
        self.assertEqual(util.human_bytes(0), "0B")
        self.assertEqual(util.human_bytes(1023), "1023B")
        self.assertEqual(util.human_bytes(1024), "1.0KB")
        self.assertEqual(util.human_bytes(1024 * 1024 * 3), "3.0MB")


class RedactTest(unittest.TestCase):
    def test_redact_env_value(self):
        with lib.env_guard(ARK_TEST_TOKEN="super" + "secretvalue123"):
            self.assertEqual(util.redact("before supersecretvalue123 after"), "before *** after")

    def test_redact_short_value_untouched(self):
        with lib.env_guard(ARK_TEST_TOKEN="abc"):
            self.assertEqual(util.redact("value abc stays"), "value abc stays")

    def test_redact_token_shape(self):
        sample = "ghp_" + "a" * 36
        self.assertEqual(util.redact("token " + sample), "token ***")

    def test_redact_obj_recurses(self):
        with lib.env_guard(ARK_TEST_TOKEN="super" + "secretvalue123"):
            cleaned = util.redact_obj({"a": ["supersecretvalue123"], "b": 1})
        self.assertEqual(cleaned, {"a": ["***"], "b": 1})

    def test_secret_env_name_detection(self):
        self.assertTrue(util.is_secret_env_name("ARK_OSS_ACCESS_KEY_SECRET"))
        self.assertTrue(util.is_secret_env_name("EMAIL_PASSWORD"))
        self.assertTrue(util.is_secret_env_name("ARK_STORAGE_SSH_PASS"))
        self.assertFalse(util.is_secret_env_name("ARK_ASSET_CACHE"))


class FileTest(unittest.TestCase):
    def test_atomic_write(self):
        with lib.temp_dir() as tmp:
            target = os.path.join(tmp, "nested", "file.json")
            util.atomic_write_json(target, {"a": 1})
            self.assertEqual(lib.read_json(target), {"a": 1})
            leftovers = [name for name in os.listdir(os.path.dirname(target)) if name.startswith(".asset-tools-")]
            self.assertEqual(leftovers, [])

    def test_safe_filename_rejects_traversal(self):
        for bad in ("../evil", "/abs/evil", "a/b", "..", ""):
            with self.assertRaises(util.PathForbidden):
                util.safe_filename(bad)

    def test_credential_filename_detection(self):
        for name in ("auth.json", "server.pem", "client.pfx", "id_rsa", ".git-credentials"):
            self.assertTrue(util.is_credential_filename(name), name)
        self.assertFalse(util.is_credential_filename("fw.bin"))
        self.assertFalse(util.is_credential_filename("manifest.json"))

    def test_safe_rel_path_rejects_escape(self):
        self.assertEqual(util.safe_rel_path("tools/x.bin"), "tools/x.bin")
        for bad in ("../x", "/etc/passwd", "a/../../b", ""):
            with self.assertRaises(util.PathForbidden):
                util.safe_rel_path(bad)


if __name__ == "__main__":
    unittest.main()
