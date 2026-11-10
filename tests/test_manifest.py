# -*- coding: utf-8 -*-
"""manifest：三种 schema 互转、校验、合并、幂等。"""

import copy
import os
import unittest

import ark_testlib as lib
from asset_tools import manifest as mf

SHA_A = "a" * 64
SHA_B = "b" * 64

CANONICAL = os.path.join(lib.FIXTURES_DIR, "manifest-canonical.json")
ARK_MESH = os.path.join(lib.FIXTURES_DIR, "manifest-ark-mesh.json")
VENUS = os.path.join(lib.FIXTURES_DIR, "manifest-venusahisiburner.json")


def asset_dict(**overrides):
    payload = {
        "id": "fixture",
        "kind": "firmware",
        "name": "Fixture",
        "version": "1.0.0",
        "filename": "x.bin",
        "bytes": 10,
        "sha256": SHA_A,
        "project": "proj",
        "sources": [{"type": "local", "path": "/tmp/x.bin", "priority": 5}],
        "install": {"cache_rel": mf.cache_rel_for(SHA_A, "x.bin"), "material_rel": "x.bin"},
        "provenance": {"origin": "local", "note": "", "discovered_at": "2026-11-10 10:00:00"},
        "status": "verified",
    }
    payload.update(overrides)
    return payload


class AssetTest(unittest.TestCase):
    def test_cache_rel_for_layout(self):
        self.assertEqual(
            mf.cache_rel_for(SHA_A, "x.bin"),
            "sha256/aa/aa/{0}/x.bin".format(SHA_A),
        )

    def test_from_dict_to_dict_roundtrip(self):
        asset = mf.Asset.from_dict(asset_dict())
        self.assertEqual(asset.id, "fixture")
        self.assertEqual(asset.to_dict()["filename"], "x.bin")
        self.assertEqual(asset.cache_rel, mf.cache_rel_for(SHA_A, "x.bin"))

    def test_extra_keys_preserved(self):
        payload = asset_dict(target_chip="EXAMPLE/CHIP")
        asset = mf.Asset.from_dict(payload)
        self.assertEqual(asset.extra["target_chip"], "EXAMPLE/CHIP")
        self.assertEqual(asset.to_dict()["target_chip"], "EXAMPLE/CHIP")

    def test_missing_cache_rel_is_filled(self):
        payload = asset_dict()
        payload["install"] = {"material_rel": "x.bin"}
        asset = mf.Asset.from_dict(payload)
        self.assertEqual(asset.install["cache_rel"], mf.cache_rel_for(SHA_A, "x.bin"))

    def test_ordered_sources(self):
        payload = asset_dict(
            sources=[
                {"type": "oss", "key": "k", "priority": 20},
                {"type": "local", "path": "/tmp/x", "priority": 5},
            ]
        )
        asset = mf.Asset.from_dict(payload)
        self.assertEqual([entry["type"] for entry in asset.ordered_sources()], ["local", "oss"])


class LoadTest(unittest.TestCase):
    def test_load_canonical_fixture(self):
        manifest = mf.AssetManifest.load(CANONICAL)
        self.assertEqual(manifest.schema, mf.SCHEMA)
        self.assertEqual(len(manifest.assets), 1)
        self.assertEqual(manifest.assets[0].id, "fixture-firmware")
        self.assertEqual(manifest.validate(), [])

    def test_load_any_canonical(self):
        manifest = mf.load_any(CANONICAL)
        self.assertEqual(manifest.assets[0].filename, "sample-firmware.txt")

    def test_load_any_ark_mesh(self):
        manifest = mf.load_any(ARK_MESH)
        self.assertEqual(len(manifest.assets), 1)
        asset = manifest.assets[0]
        self.assertEqual(asset.kind, "other")
        self.assertEqual(asset.sha256, lib.sha256_file(lib.TOOL_FIXTURE))
        self.assertEqual(asset.sources[0]["type"], "node-storage")
        self.assertEqual(asset.sources[0]["node"], "storage-example")
        self.assertEqual(manifest.validate(), [])

    def test_load_any_venusahisiburner(self):
        manifest = mf.load_any(VENUS)
        asset = manifest.assets[0]
        self.assertEqual(asset.filename, "sample-tool.txt")
        self.assertEqual(asset.sources[0]["type"], "oss")
        self.assertEqual(asset.sources[0]["key"], "fixture/sample-tool.txt")
        self.assertEqual(asset.cache_rel, mf.cache_rel_for(lib.sha256_file(lib.TOOL_FIXTURE), "sample-tool.txt"))
        self.assertEqual(asset.extra["legacy_cache_rel"], "tools/fixture/sample-tool.txt")
        self.assertEqual(manifest.validate(), [])

    def test_load_any_unknown_schema(self):
        with lib.temp_dir() as tmp:
            path = os.path.join(tmp, "bad.json")
            lib.write_json(path, {"schema": "nope/v1", "assets": []})
            with self.assertRaises(ValueError):
                mf.load_any(path)

    def test_save_and_reload_idempotent(self):
        manifest = mf.AssetManifest.load(CANONICAL)
        with lib.temp_dir() as tmp:
            target = os.path.join(tmp, "out.json")
            manifest.save(target)
            again = mf.AssetManifest.load(target)
        self.assertEqual(again.dumps(), manifest.dumps())
        self.assertEqual(again.validate(), [])


class ValidateTest(unittest.TestCase):
    def _manifest(self, **overrides):
        return mf.AssetManifest.from_dict(
            {
                "schema": mf.SCHEMA,
                "generated_at": "2026-11-10 10:00:00",
                "assets": [asset_dict(**overrides)],
            }
        )

    def test_valid_manifest(self):
        self.assertEqual(self._manifest().validate(), [])

    def test_bad_sha256(self):
        errors = self._manifest(sha256="not-a-hash").validate()
        self.assertTrue(any("sha256" in message for message in errors))

    def test_cache_rel_mismatch(self):
        errors = self._manifest(install={"cache_rel": "tools/x.bin", "material_rel": "x.bin"}).validate()
        self.assertTrue(any("cache_rel" in message for message in errors))

    def test_duplicate_id(self):
        payload = {
            "schema": mf.SCHEMA,
            "generated_at": "2026-11-10 10:00:00",
            "assets": [asset_dict(), asset_dict()],
        }
        errors = mf.AssetManifest.from_dict(payload).validate()
        self.assertTrue(any("重复" in message for message in errors))

    def test_bad_kind_and_status(self):
        errors = self._manifest(kind="rom", status="done").validate()
        self.assertTrue(any("kind" in message for message in errors))
        self.assertTrue(any("status" in message for message in errors))

    def test_bad_source_type(self):
        errors = self._manifest(sources=[{"type": "ftp", "path": "/x"}]).validate()
        self.assertTrue(any("sources" in message for message in errors))

    def test_by_id(self):
        manifest = mf.AssetManifest.load(CANONICAL)
        self.assertIsNotNone(manifest.by_id("fixture-firmware"))
        self.assertIsNone(manifest.by_id("nope"))


class MergeTest(unittest.TestCase):
    def test_same_sha_merges_sources(self):
        left = mf.AssetManifest.from_dict(
            {
                "schema": mf.SCHEMA,
                "assets": [asset_dict(sources=[{"type": "local", "path": "/a", "priority": 9}])],
            }
        )
        right = mf.AssetManifest.from_dict(
            {
                "schema": mf.SCHEMA,
                "assets": [
                    asset_dict(
                        sources=[
                            {"type": "local", "path": "/a", "priority": 3},
                            {"type": "oss", "key": "k/x.bin", "priority": 20},
                        ]
                    )
                ],
            }
        )
        merged = left.merge(right)
        sources = merged.assets[0].sources
        self.assertEqual(len(sources), 2)
        local = [item for item in sources if item["type"] == "local"][0]
        self.assertEqual(local["priority"], 3)
        self.assertEqual(merged.assets[0].sha256, SHA_A)

    def test_conflict_prefer_newest(self):
        older = asset_dict(
            sha256=SHA_A,
            provenance={"origin": "local", "note": "", "discovered_at": "2026-11-10 09:00:00"},
        )
        newer = asset_dict(
            sha256=SHA_B,
            provenance={"origin": "oss", "note": "", "discovered_at": "2026-11-10 12:00:00"},
        )
        merged = mf.AssetManifest.from_dict({"schema": mf.SCHEMA, "assets": [older]}).merge(
            mf.AssetManifest.from_dict({"schema": mf.SCHEMA, "assets": [newer]})
        )
        asset = merged.assets[0]
        self.assertEqual(asset.sha256, SHA_B)
        self.assertTrue(asset.provenance["note"].startswith("superseded:"))

    def test_conflict_prefer_self(self):
        older = asset_dict(sha256=SHA_A)
        newer = asset_dict(
            sha256=SHA_B,
            provenance={"origin": "oss", "note": "", "discovered_at": "2026-11-10 12:00:00"},
        )
        merged = mf.AssetManifest.from_dict({"schema": mf.SCHEMA, "assets": [older]}).merge(
            mf.AssetManifest.from_dict({"schema": mf.SCHEMA, "assets": [newer]}), prefer="self"
        )
        self.assertEqual(merged.assets[0].sha256, SHA_A)
        self.assertTrue(merged.assets[0].provenance["note"].startswith("superseded:"))

    def test_merge_adds_new_asset(self):
        left = mf.AssetManifest.from_dict({"schema": mf.SCHEMA, "assets": [asset_dict()]})
        right = mf.AssetManifest.from_dict(
            {"schema": mf.SCHEMA, "assets": [asset_dict(id="second", filename="y.bin")]}
        )
        merged = left.merge(right)
        self.assertEqual(merged.ids(), ["fixture", "second"])

    def test_merge_is_idempotent(self):
        left = mf.AssetManifest.load(CANONICAL)
        once = left.merge(left)
        twice = once.merge(left)
        self.assertEqual([a.to_dict() for a in once.assets], [a.to_dict() for a in twice.assets])


class ConvertTest(unittest.TestCase):
    def test_to_ark_artifacts(self):
        document = mf.AssetManifest.load(CANONICAL).to_ark_artifacts()
        self.assertEqual(document["schema"], mf.SCHEMA_ARK_MESH)
        artifact = document["artifacts"][0]
        self.assertEqual(artifact["sha256"], lib.sha256_file(lib.FIRMWARE_FIXTURE))
        self.assertEqual(artifact["destination"], "fixture/sample-firmware.txt")

    def test_to_venusahisiburner(self):
        document = mf.AssetManifest.load(CANONICAL).to_venusahisiburner(description="demo")
        self.assertEqual(document["schema"], mf.SCHEMA_VENUSAHISIBURNER)
        self.assertEqual(document["description"], "demo")
        entry = document["assets"][0]
        self.assertEqual(entry["file"], "sample-firmware.txt")
        self.assertIn("material_rel", entry["install"])

    def test_ark_mesh_roundtrip_through_canonical(self):
        manifest = mf.load_any(ARK_MESH)
        document = manifest.to_ark_artifacts()
        self.assertEqual(len(document["artifacts"]), 1)
        self.assertEqual(document["artifacts"][0]["source_node"], "storage-example")


if __name__ == "__main__":
    unittest.main()
