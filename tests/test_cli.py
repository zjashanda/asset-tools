# -*- coding: utf-8 -*-
"""CLI：各子命令退出码、--json 输出可解析、dry-run / apply 语义。"""

import contextlib
import io
import json
import os
import shutil
import unittest

import ark_testlib as lib
from asset_tools.cli import main


def call(args):
    """进程内执行 CLI，返回 (退出码, stdout)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue()


def call_json(args):
    code, stdout = call(args)
    assert stdout.strip(), "stdout 为空"
    return code, json.loads(stdout)


class CliBase(unittest.TestCase):
    def setUp(self):
        self._tmp = lib.temp_dir()
        self.tmp = self._tmp.__enter__()
        self.cache = os.path.join(self.tmp, "cache")
        self.dest = os.path.join(self.tmp, "dest")
        self.src = os.path.join(self.tmp, "sample-firmware.txt")
        shutil.copyfile(lib.FIRMWARE_FIXTURE, self.src)
        self.manifest = os.path.join(self.tmp, "asset-manifest.json")
        lib.write_json(self.manifest, lib.manifest_doc(lib.asset_doc(self.src, asset_id="fixture")))
        self._env = lib.env_guard(ARK_ASSET_CACHE=self.cache, ARK_ASSET_MANIFEST=self.manifest)
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__(None, None, None)
        self._tmp.__exit__(None, None, None)

    def args(self, *rest):
        return list(rest) + ["--cache", self.cache]


class StatusCliTest(CliBase):
    def test_status_json(self):
        code, payload = call_json(self.args("status", "--json", "--manifest", self.manifest))
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], "ark.status.v1")
        self.assertEqual(len(payload["adapters"]), 6)
        names = [item["name"] for item in payload["adapters"]]
        self.assertEqual(names, ["local", "http", "oss", "node-storage", "email", "nas"])
        self.assertIn("cache", payload)
        self.assertTrue(payload["manifest"]["exists"])

    def test_status_human(self):
        code, stdout = call(self.args("status", "--manifest", self.manifest))
        self.assertEqual(code, 0)
        self.assertIn("adapter", stdout)
        self.assertIn("local", stdout)

    def test_status_default_has_no_rfc3339_field(self):
        code, payload = call_json(self.args("status", "--json", "--manifest", self.manifest))
        self.assertEqual(code, 0)
        self.assertNotIn("generated_at_rfc3339", payload)
        self.assertRegex(payload["generated_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_status_legacy_rfc3339_is_opt_in(self):
        code, payload = call_json(
            self.args("status", "--json", "--manifest", self.manifest, "--legacy-rfc3339")
        )
        self.assertEqual(code, 0)
        self.assertRegex(
            payload["generated_at_rfc3339"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+0800$",  # time-lint:allow legacy opt-in
        )

    def test_status_json_has_no_credentials(self):
        code, stdout = call(self.args("status", "--json", "--manifest", self.manifest))
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        for item in payload["adapters"]:
            self.assertNotIn("password", json.dumps(item).lower())
            for name in item["missing_env"]:
                self.assertRegex(name, r"^[A-Z0-9_]+$")


class PlanCliTest(CliBase):
    def test_plan_json(self):
        code, payload = call_json(self.args("plan", "--manifest", self.manifest, "--json"))
        self.assertEqual(code, 0)
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["summary"]["assets"], 1)
        self.assertEqual(payload["assets"][0]["action"], "fetch")
        self.assertEqual(payload["assets"][0]["sources"][0]["type"], "local")

    def test_plan_with_cached_asset(self):
        code, _ = call_json(self.args("fetch", "--manifest", self.manifest, "--asset", "fixture", "--apply", "--json"))
        self.assertEqual(code, 0)
        code, payload = call_json(self.args("plan", "--manifest", self.manifest, "--json"))
        self.assertEqual(payload["assets"][0]["action"], "use-cache")

    def test_plan_missing_manifest(self):
        code, _stdout = call(self.args("plan", "--manifest", os.path.join(self.tmp, "nope.json"), "--json"))
        self.assertEqual(code, 3)

    def test_plan_missing_required_arg(self):
        code, _stdout = call(["plan"])
        self.assertEqual(code, 2)


class DiscoverCliTest(CliBase):
    def test_discover_blocked_provider(self):
        code, payload = call_json(self.args("discover", "--provider", "oss", "--json"))
        self.assertEqual(code, 3)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "BLOCKED:ADAPTER_UNAVAILABLE")
        self.assertEqual(payload["assets"], [])
        self.assertFalse(payload["applied"])

    def test_discover_local(self):
        code, payload = call_json(
            self.args("discover", "--provider", "local", "--query", "paths=" + self.src, "--json")
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["assets"][0]["filename"], "sample-firmware.txt")

    def test_discover_unknown_provider(self):
        code, _stdout = call(self.args("discover", "--provider", "ftp", "--json"))
        self.assertEqual(code, 2)

    def test_discover_bad_query(self):
        code, _stdout = call(self.args("discover", "--provider", "local", "--query", "oops", "--json"))
        self.assertEqual(code, 2)


class RegisterCliTest(CliBase):
    def test_register_dry_run(self):
        target = os.path.join(self.tmp, "new-manifest.json")
        code, payload = call_json(
            self.args(
                "register",
                "--manifest",
                target,
                "--file",
                self.src,
                "--id",
                "reg-fixture",
                "--kind",
                "firmware",
                "--json",
            )
        )
        self.assertEqual(code, 0)
        self.assertFalse(payload["applied"])
        self.assertFalse(os.path.exists(target))
        self.assertEqual(payload["outcome"], "added")
        self.assertEqual(payload["asset"]["sha256"], lib.sha256_file(self.src))

    def test_register_apply_then_validate(self):
        target = os.path.join(self.tmp, "new-manifest.json")
        code, _ = call_json(
            self.args(
                "register",
                "--manifest",
                target,
                "--file",
                self.src,
                "--id",
                "reg-fixture",
                "--kind",
                "firmware",
                "--apply",
                "--json",
            )
        )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(target))
        code, payload = call_json(
            self.args("manifest", "validate", "--manifest", target, "--json")
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])

    def test_register_deduplicates(self):
        code, first = call_json(
            self.args(
                "register",
                "--manifest",
                self.manifest,
                "--file",
                self.src,
                "--id",
                "fixture",
                "--kind",
                "firmware",
                "--apply",
                "--json",
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(first["outcome"], "merged")

    def test_register_bad_kind(self):
        code, _stdout = call(
            self.args(
                "register",
                "--manifest",
                self.manifest,
                "--file",
                self.src,
                "--id",
                "x",
                "--kind",
                "rom",
                "--json",
            )
        )
        self.assertEqual(code, 2)

    def test_register_rejects_credential_filename(self):
        credential = os.path.join(self.tmp, "auth.json")
        with open(credential, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        code, _stdout = call(
            self.args(
                "register",
                "--manifest",
                os.path.join(self.tmp, "cred-manifest.json"),
                "--file",
                credential,
                "--id",
                "cred",
                "--kind",
                "other",
                "--json",
            )
        )
        self.assertEqual(code, 4)

    def test_register_missing_file(self):
        code, _stdout = call(
            self.args(
                "register",
                "--manifest",
                self.manifest,
                "--file",
                os.path.join(self.tmp, "nope.bin"),
                "--id",
                "x",
                "--kind",
                "tool",
                "--json",
            )
        )
        self.assertEqual(code, 3)


class VerifyCliTest(CliBase):
    def test_verify_cache_missing(self):
        code, payload = call_json(self.args("verify", "--manifest", self.manifest, "--json"))
        self.assertEqual(code, 1)
        self.assertEqual(payload["assets"][0]["state"], "MISSING")

    def test_verify_after_fetch(self):
        call_json(self.args("fetch", "--manifest", self.manifest, "--asset", "fixture", "--apply", "--json"))
        code, payload = call_json(self.args("verify", "--manifest", self.manifest, "--json"))
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])

    def test_verify_dir(self):
        os.makedirs(self.dest)
        shutil.copyfile(self.src, os.path.join(self.dest, "sample-firmware.txt"))
        code, payload = call_json(
            self.args("verify", "--manifest", self.manifest, "--dir", self.dest, "--json")
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["scope"], "dir")

    def test_verify_unknown_asset(self):
        code, _stdout = call(self.args("verify", "--manifest", self.manifest, "--asset", "nope", "--json"))
        self.assertEqual(code, 3)


class CacheCliTest(CliBase):
    def test_cache_list_empty(self):
        code, payload = call_json(self.args("cache", "list", "--json"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 0)

    def test_cache_prune_dry_run(self):
        code, payload = call_json(self.args("cache", "prune", "--keep-days", "7", "--json"))
        self.assertEqual(code, 0)
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["keep_days"], 7)

    def test_cache_prune_keep_days_zero_is_honored(self):
        code, payload = call_json(self.args("cache", "prune", "--keep-days", "0", "--json"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["keep_days"], 0)

    def test_cache_verify_after_fetch(self):
        call_json(self.args("fetch", "--manifest", self.manifest, "--asset", "fixture", "--apply", "--json"))
        code, payload = call_json(self.args("cache", "verify", "--json"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["bad"], 0)
        self.assertEqual(payload["count"], 1)

    def test_cache_without_action(self):
        code, _stdout = call(self.args("cache", "--json"))
        self.assertEqual(code, 2)


class SupplyCliTest(CliBase):
    def test_supply_dry_run(self):
        code, payload = call_json(
            self.args("supply", "--manifest", self.manifest, "--asset", "fixture", "--dest", self.dest, "--json")
        )
        self.assertEqual(code, 0)
        self.assertFalse(payload["applied"])
        self.assertFalse(os.path.exists(self.dest))

    def test_supply_apply(self):
        code, payload = call_json(
            self.args(
                "supply",
                "--manifest",
                self.manifest,
                "--asset",
                "fixture",
                "--dest",
                self.dest,
                "--apply",
                "--json",
            )
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["applied"])
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "sample-firmware.txt")))
        record = lib.read_json(os.path.join(self.dest, ".ark-supply", "fixture.json"))
        self.assertFalse(record["is_receipt"])

    def test_supply_unknown_asset(self):
        code, _stdout = call(
            self.args("supply", "--manifest", self.manifest, "--asset", "nope", "--dest", self.dest, "--json")
        )
        self.assertEqual(code, 2)


class ManifestCliTest(CliBase):
    def test_manifest_show(self):
        code, payload = call_json(self.args("manifest", "show", "--manifest", self.manifest, "--json"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["assets"][0]["id"], "fixture")

    def test_manifest_validate_ok(self):
        code, payload = call_json(self.args("manifest", "validate", "--manifest", self.manifest, "--json"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["errors"], [])

    def test_manifest_validate_fail(self):
        bad = os.path.join(self.tmp, "bad.json")
        payload = lib.manifest_doc(lib.asset_doc(self.src, asset_id="fixture"))
        payload["assets"][0]["sha256"] = "zz"
        lib.write_json(bad, payload)
        code, result = call_json(self.args("manifest", "validate", "--manifest", bad, "--json"))
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_manifest_convert_ark_artifacts(self):
        code, payload = call_json(
            self.args("manifest", "convert", "--manifest", self.manifest, "--to", "ark-artifacts", "--json")
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], "ark-artifact-manifest.v1")
        self.assertEqual(len(payload["document"]["artifacts"]), 1)

    def test_manifest_convert_venusahisiburner(self):
        code, payload = call_json(
            self.args(
                "manifest", "convert", "--manifest", self.manifest, "--to", "venusahisiburner", "--json"
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["document"]["schema"], "venusahisiburner-assets/v1")

    def test_manifest_convert_requires_to(self):
        code, _stdout = call(self.args("manifest", "convert", "--manifest", self.manifest, "--json"))
        self.assertEqual(code, 2)

    def test_manifest_merge(self):
        other = os.path.join(self.tmp, "other.json")
        lib.write_json(
            other,
            lib.manifest_doc(lib.asset_doc(lib.TOOL_FIXTURE, asset_id="other-tool", kind="tool")),
        )
        code, payload = call_json(
            self.args("manifest", "merge", "--manifest", self.manifest, "--other", other, "--json")
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 2)


class WrapperTest(CliBase):
    def test_bin_wrapper_status(self):
        completed = lib.run_bin(["status", "--json", "--manifest", self.manifest])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema"], "ark.status.v1")

    def test_module_entrypoint_help(self):
        completed = lib.run_module(["--help"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("supply", completed.stdout)

    def test_unknown_command(self):
        code, _stdout = call(["nonsense", "--json"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
