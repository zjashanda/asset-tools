# -*- coding: utf-8 -*-
"""cache：内容寻址、put/verify、隔离、prune、路径穿越拒绝。"""

import os
import shutil
import time
import unittest
from datetime import timedelta

import ark_testlib as lib
from asset_tools import cache as cache_mod
from asset_tools import manifest as mf
from asset_tools import util


def make_asset(path, asset_id="fixture", **overrides):
    return mf.Asset.from_dict(lib.asset_doc(path, asset_id=asset_id, **overrides))


class CachePathTest(unittest.TestCase):
    def test_path_for_layout(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(tmp)
            asset = make_asset(lib.FIRMWARE_FIXTURE)
            target = cache.path_for(asset)
            expected = os.path.join(tmp, "sha256", asset.sha256[:2], asset.sha256[2:4], asset.sha256, asset.filename)
            self.assertEqual(str(target), os.path.realpath(expected))

    def test_rejects_path_traversal(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(tmp)
            asset = make_asset(lib.FIRMWARE_FIXTURE)
            asset.filename = "../evil.bin"
            with self.assertRaises(util.PathForbidden):
                cache.path_for(asset)

    def test_rejects_absolute_filename(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(tmp)
            asset = make_asset(lib.FIRMWARE_FIXTURE)
            asset.filename = "/etc/passwd"
            with self.assertRaises(util.PathForbidden):
                cache.path_for(asset)

    def test_env_root(self):
        with lib.temp_dir() as tmp:
            with lib.env_guard(ARK_ASSET_CACHE=tmp):
                cache = cache_mod.ContentCache()
            self.assertEqual(str(cache.root), os.path.realpath(tmp))


class PutVerifyTest(unittest.TestCase):
    def test_has_false_before_true_after(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            src = os.path.join(tmp, "src.bin")
            shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
            asset = make_asset(src)
            self.assertFalse(cache.has(asset))
            result = cache.put(asset, src, apply=True)
            self.assertTrue(result.ok)
            self.assertTrue(result.applied)
            self.assertTrue(cache.has(asset))
            self.assertEqual(util.sha256_file(result.path), asset.sha256)

    def test_put_dry_run_does_not_write(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            src = os.path.join(tmp, "src.bin")
            shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
            asset = make_asset(src)
            result = cache.put(asset, src, apply=False)
            self.assertTrue(result.ok)
            self.assertFalse(result.applied)
            self.assertFalse(os.path.exists(result.path))
            self.assertFalse(os.path.exists(os.path.join(tmp, "cache", "_incoming")))

    def test_put_rejects_sha_mismatch(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            src = os.path.join(tmp, "src.bin")
            shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
            asset = make_asset(src)
            asset.sha256 = "c" * 64
            result = cache.put(asset, src, apply=True)
            self.assertFalse(result.ok)
            self.assertEqual(result.reason, "SHA256_MISMATCH")
            self.assertEqual(result.exit_code, util.EXIT_FAIL)
            self.assertFalse(os.path.exists(cache.root / "sha256"))

    def test_put_missing_source(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            asset = make_asset(lib.FIRMWARE_FIXTURE)
            result = cache.put(asset, os.path.join(tmp, "nope.bin"), apply=True)
            self.assertFalse(result.ok)
            self.assertEqual(result.reason, "SOURCE_MISSING")
            self.assertEqual(result.exit_code, util.EXIT_NO_SOURCE)

    def test_verify_states(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            src = os.path.join(tmp, "src.bin")
            shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
            asset = make_asset(src)
            self.assertEqual(cache.verify(asset), cache_mod.VERIFY_MISSING)
            cache.put(asset, src, apply=True)
            self.assertEqual(cache.verify(asset), cache_mod.VERIFY_OK)
            with open(cache.path_for(asset), "wb") as handle:
                handle.write(b"corrupted")
            self.assertEqual(cache.verify(asset), cache_mod.VERIFY_CORRUPT)
            # apply=False 不移动文件
            self.assertTrue(cache.path_for(asset).is_file())

    def test_verify_corrupt_quarantines(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            src = os.path.join(tmp, "src.bin")
            shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
            asset = make_asset(src)
            cache.put(asset, src, apply=True)
            with open(cache.path_for(asset), "wb") as handle:
                handle.write(b"corrupted")
            state = cache.verify(asset, apply=True)
            self.assertEqual(state, cache_mod.VERIFY_CORRUPT)
            self.assertFalse(cache.path_for(asset).exists())
            quarantined = list((cache.root / "_quarantine").glob("*" + asset.filename))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(cache.last_quarantine, str(quarantined[0]))

    def test_stats(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            src = os.path.join(tmp, "src.bin")
            shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
            asset = make_asset(src)
            cache.put(asset, src, apply=True)
            stats = cache.stats()
            self.assertEqual(stats["entries"], 1)
            self.assertEqual(stats["total_bytes"], os.path.getsize(src))
            self.assertEqual(stats["quarantine_files"], 0)


class ListPruneTest(unittest.TestCase):
    def _seed(self, cache, src, asset):
        cache.put(asset, src, apply=True)
        return cache.path_for(asset)

    def test_list_entries(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            src = os.path.join(tmp, "src.bin")
            shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
            asset = make_asset(src)
            self._seed(cache, src, asset)
            entries = cache.list_entries(deep=True)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].sha256, asset.sha256)
            self.assertTrue(entries[0].verified)
            self.assertEqual(entries[0].filename, asset.filename)

    def test_prune_dry_run_lists_but_keeps(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            src = os.path.join(tmp, "src.bin")
            shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
            asset = make_asset(src)
            target = self._seed(cache, src, asset)
            old = time.time() - 10 * 24 * 3600
            os.utime(target, (old, old))
            plan = cache.prune(keep_days=1, apply=False)
            self.assertFalse(plan.applied)
            self.assertEqual(len(plan.entries), 1)
            self.assertEqual(plan.total_bytes, os.path.getsize(src))
            self.assertTrue(target.is_file())

    def test_prune_apply_removes(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            src = os.path.join(tmp, "src.bin")
            shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
            asset = make_asset(src)
            target = self._seed(cache, src, asset)
            old = time.time() - 10 * 24 * 3600
            os.utime(target, (old, old))
            plan = cache.prune(keep_days=1, apply=True)
            self.assertTrue(plan.applied)
            self.assertEqual(plan.removed, 1)
            self.assertFalse(target.exists())

    def test_prune_keeps_fresh_entries(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            src = os.path.join(tmp, "src.bin")
            shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
            asset = make_asset(src)
            self._seed(cache, src, asset)
            plan = cache.prune(keep_days=30, apply=True)
            self.assertEqual(plan.entries, [])

    def test_prune_counts_quarantine_area(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            quarantine = cache.root / "_quarantine"
            quarantine.mkdir(parents=True)
            stale = quarantine / "20260101-000000-old.bin"
            stale.write_bytes(b"0123456789")
            old = time.time() - 40 * 24 * 3600
            os.utime(stale, (old, old))
            plan = cache.prune(keep_days=30, apply=False)
            self.assertEqual(plan.area_count("_quarantine"), 1)
            self.assertEqual(plan.area_bytes("_quarantine"), 10)
            self.assertEqual(plan.total_bytes, 10)

    def test_prune_negative_days_rejected(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            with self.assertRaises(ValueError):
                cache.prune(keep_days=-1)

    def test_prune_with_explicit_now(self):
        with lib.temp_dir() as tmp:
            cache = cache_mod.ContentCache(os.path.join(tmp, "cache"))
            src = os.path.join(tmp, "src.bin")
            shutil.copyfile(lib.FIRMWARE_FIXTURE, src)
            asset = make_asset(src)
            target = self._seed(cache, src, asset)
            moment = util.now_beijing() + timedelta(days=90)
            plan = cache.prune(keep_days=30, now=moment, apply=False)
            self.assertEqual(len(plan.entries), 1)
            self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
