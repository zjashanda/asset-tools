# -*- coding: utf-8 -*-
"""supply：dry-run 不写盘、apply 落位 + 记录、is_receipt=false。"""

import json
import os
import shutil
import unittest

import ark_testlib as lib
from asset_tools import cache as cache_mod
from asset_tools import manifest as mf
from asset_tools import supply as supply_mod
from asset_tools import util


def build_manifest(tmp, material_rel=None, sha256=None, sources=None, asset_id="fixture"):
    src = os.path.join(tmp, "src", "sample-firmware.txt")
    if not os.path.isfile(src):
        os.makedirs(os.path.dirname(src))
        shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
    payload = lib.asset_doc(
        src,
        asset_id=asset_id,
        material_rel=material_rel,
        sources=sources if sources is not None else [{"type": "local", "path": src, "priority": 5}],
    )
    if sha256:
        payload["sha256"] = sha256
    return mf.AssetManifest.from_dict(lib.manifest_doc(payload)), src


class SupplyDryRunTest(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(tmp)
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            dest = os.path.join(tmp, "dest")
            result = supply_mod.supply_asset(manifest, "fixture", dest, cache, apply=False)
            self.assertTrue(result.ok)
            self.assertFalse(result.applied)
            self.assertEqual(result.reason, "DRY_RUN")
            self.assertFalse(os.path.exists(dest))
            self.assertFalse(os.path.exists(os.path.join(tmp, "cache")))
            self.assertEqual(result.attempts[0]["type"], "local")

    def test_dry_run_reports_cache_state(self):
        with lib.temp_dir() as tmp:
            manifest, src = build_manifest(tmp)
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            cache.put(manifest.assets[0], src, apply=True)
            result = supply_mod.supply_asset(
                manifest, "fixture", os.path.join(tmp, "dest"), cache, apply=False
            )
            self.assertEqual(result.cache_state, "OK")
            self.assertFalse(result.applied)


class SupplyApplyTest(unittest.TestCase):
    def test_apply_copies_and_records(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(tmp)
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            dest = os.path.join(tmp, "dest")
            result = supply_mod.supply_asset(manifest, "fixture", dest, cache, apply=True)
            self.assertTrue(result.ok, result.detail)
            self.assertTrue(result.applied)
            asset = manifest.assets[0]
            target = os.path.join(dest, asset.filename)
            self.assertTrue(os.path.isfile(target))
            self.assertEqual(util.sha256_file(target), asset.sha256)
            self.assertTrue(result.dest_sha256_verified)
            self.assertTrue(os.path.isfile(result.record_path))
            record = lib.read_json(result.record_path)
            self.assertEqual(record["schema"], "ark.supply-record.v1")
            self.assertEqual(record["asset_id"], "fixture")
            self.assertEqual(record["sha256"], asset.sha256)
            self.assertEqual(record["dest_sha256"], asset.sha256)
            self.assertTrue(record["dest_sha256_verified"])
            self.assertEqual(record["archive_owner"], "ark-archive")
            self.assertFalse(record["is_receipt"])
            self.assertEqual(record["source_type"], "local")
            self.assertRegex(record["supplied_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_material_rel_subdirectory(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(tmp, material_rel="tools/sub/sample-firmware.txt")
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            dest = os.path.join(tmp, "dest")
            result = supply_mod.supply_asset(manifest, "fixture", dest, cache, apply=True)
            self.assertTrue(result.ok, result.detail)
            self.assertTrue(os.path.isfile(os.path.join(dest, "tools", "sub", "sample-firmware.txt")))
            # 记录仍写在 dest/.ark-supply
            self.assertTrue(
                os.path.isfile(os.path.join(dest, ".ark-supply", "fixture.json"))
            )

    def test_record_dir_override(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(tmp)
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            dest = os.path.join(tmp, "dest")
            records = os.path.join(tmp, "records")
            result = supply_mod.supply_asset(
                manifest, "fixture", dest, cache, apply=True, record_dir=records
            )
            self.assertTrue(result.ok, result.detail)
            self.assertTrue(os.path.isfile(os.path.join(records, "fixture.json")))

    def test_unknown_asset(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(tmp)
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            result = supply_mod.supply_asset(manifest, "nope", tmp, cache, apply=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, util.EXIT_USAGE)
        self.assertEqual(result.reason, "ASSET_NOT_FOUND")

    def test_traversal_material_rel_forbidden(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(tmp)
            manifest.assets[0].install["material_rel"] = "../escape.bin"
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            result = supply_mod.supply_asset(
                manifest, "fixture", os.path.join(tmp, "dest"), cache, apply=True
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, util.EXIT_FORBIDDEN)

    def test_source_sha_mismatch_fails(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(tmp)
            manifest.assets[0].sha256 = "f" * 64
            manifest.assets[0].install["cache_rel"] = mf.cache_rel_for("f" * 64, manifest.assets[0].filename)
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            result = supply_mod.supply_asset(
                manifest, "fixture", os.path.join(tmp, "dest"), cache, apply=True
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, util.EXIT_FAIL)

    def test_all_sources_blocked(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(
                tmp, sources=[{"type": "oss", "key": "k/sample-firmware.txt", "priority": 10}]
            )
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            with lib.env_guard():
                result = supply_mod.supply_asset(
                    manifest, "fixture", os.path.join(tmp, "dest"), cache, apply=True
                )
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, util.EXIT_NO_SOURCE)
        self.assertTrue(result.attempts)

    def test_cache_hit_used(self):
        with lib.temp_dir() as tmp:
            manifest, src = build_manifest(tmp)
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            cache.put(manifest.assets[0], src, apply=True)
            result = supply_mod.supply_asset(
                manifest, "fixture", os.path.join(tmp, "dest"), cache, apply=True
            )
            self.assertTrue(result.ok, result.detail)
            self.assertEqual(result.source_type, "cache")
            self.assertTrue(result.cache_path)


class FetchToCacheTest(unittest.TestCase):
    def test_dry_run_no_cache_dir(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(tmp)
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            result = supply_mod.fetch_to_cache(manifest, "fixture", cache, apply=False)
            self.assertFalse(result["applied"])
            self.assertEqual(result["reason"], "DRY_RUN")
            self.assertFalse(os.path.exists(os.path.join(tmp, "cache")))

    def test_apply_populates_cache(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(tmp)
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            result = supply_mod.fetch_to_cache(manifest, "fixture", cache, apply=True)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["applied"])
            self.assertEqual(result["cache_state"], "OK")
            self.assertTrue(cache.has(manifest.assets[0]))

    def test_unknown_asset(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(tmp)
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            result = supply_mod.fetch_to_cache(manifest, "nope", cache, apply=False)
        self.assertEqual(result["exit_code"], util.EXIT_USAGE)


class PlanTest(unittest.TestCase):
    def test_plan_actions(self):
        with lib.temp_dir() as tmp:
            manifest, src = build_manifest(tmp)
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            asset = manifest.assets[0]
            plan = supply_mod.plan_asset(manifest, asset, cache)
            self.assertEqual(plan["action"], "fetch")
            cache.put(asset, src, apply=True)
            plan_hit = supply_mod.plan_asset(manifest, asset, cache)
            self.assertEqual(plan_hit["action"], "use-cache")
            self.assertEqual(plan_hit["cache_state"], "OK")

    def test_plan_no_source(self):
        with lib.temp_dir() as tmp:
            manifest, _src = build_manifest(
                tmp, sources=[{"type": "oss", "key": "k/x.bin", "priority": 10}]
            )
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            with lib.env_guard():
                plan = supply_mod.plan_asset(manifest, manifest.assets[0], cache)
        self.assertEqual(plan["action"], "no-source")
        self.assertFalse(plan["sources"][0]["available"])


if __name__ == "__main__":
    unittest.main()
