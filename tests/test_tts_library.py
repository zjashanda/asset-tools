# -*- coding: utf-8 -*-
"""1006 TTS 共享音频库核心：缓存键、命中、取件、发布、锁、outbox、ACL、索引。

全部离线、只用标准库与仓库内临时目录；不联网、不 ssh、不依赖本机 1006。
"""

import concurrent.futures
import contextlib
import io
import json
import os
import shutil
import stat
import unittest

import ark_testlib as lib
from asset_tools import tts_library as tts
from asset_tools.cli import main


def make_spec(**overrides):
    """构造一份完整合成规格；缺省参数显式写 null。"""
    text = overrides.pop("text", "你好，世界")
    language = overrides.pop("language", "zh")
    speaker = overrides.pop("speaker", "付小春")
    voice_ref = overrides.pop("voice_ref", None)
    engine = overrides.pop(
        "engine", {"engine": "lstts", "model": "IndexTTS", "model_version": "unknown"}
    )
    params = overrides.pop(
        "synthesis_params",
        {"rate": None, "pitch": None, "style": None, "emotion": None, "speed": None},
    )
    output = overrides.pop(
        "output",
        {
            "container": "wav",
            "codec": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "bit_depth": 16,
        },
    )
    spec = {
        "text": {"raw": text, "language": language},
        "voice": {"speaker": speaker, "voice_ref_sha256": voice_ref},
        "engine": engine,
        "synthesis_params": params,
        "output": output,
        "normalization_profile": overrides.pop("normalization_profile", "none"),
    }
    if overrides:
        raise AssertionError("未知覆盖项: {0}".format(sorted(overrides)))
    return spec


def audio_bytes(seed=b"A", size=800):
    """确定性伪音频字节；不要求可播放，只要求内容哈希稳定。"""
    return b"RIFF" + (seed * size)


def call_cli(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


class TtsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = lib.temp_dir("tts-library-test-")
        self.tmp = self._tmp.__enter__()
        self.root = os.path.join(self.tmp, "library")
        os.makedirs(self.root)
        self.library = tts.TtsLibrary(self.root)
        self._counter = 0

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def task_dir(self, name="NOAH-66"):
        path = os.path.join(self.tmp, "tasks", name)
        os.makedirs(path, exist_ok=True)
        return path

    def staging(self, spec, audio=None, *, name=None, **manifest_kwargs):
        self._counter += 1
        name = name or "staging-{0}".format(self._counter)
        path = os.path.join(self.tmp, "staging", name)
        os.makedirs(path, exist_ok=False)
        audio_path = os.path.join(path, "audio.wav")
        with open(audio_path, "wb") as handle:
            handle.write(audio if audio is not None else audio_bytes(name.encode("utf-8")))
        manifest = tts.manifest_from_spec(spec, audio_path, **manifest_kwargs)
        lib.write_json(os.path.join(path, "manifest.json"), manifest)
        return path, manifest

    def publish(self, spec, audio=None, *, name=None, task=None, **kwargs):
        manifest_keys = (
            "shareable",
            "scope",
            "quality",
            "status",
            "sensitivity",
            "provenance",
            "duration_seconds",
        )
        manifest_kwargs = {key: kwargs.pop(key) for key in manifest_keys if key in kwargs}
        staging, manifest = self.staging(
            spec, audio, name=name, **manifest_kwargs
        )
        result = self.library.publish(
            staging,
            task_dir=task or self.task_dir(),
            **kwargs
        )
        return result, staging, manifest

    def asset_dirs(self):
        return sorted(
            name
            for name in os.listdir(self.root)
            if os.path.isdir(os.path.join(self.root, name)) and name.startswith("tts-")
        )


class CacheKeyTest(unittest.TestCase):
    def test_cache_key_deterministic_normalization(self):
        left = make_spec(text="\u200b你好，\u3000  世界", language="zh")
        right = make_spec(text="你好， 世界", language="ZH")
        first = tts.compute_key(left)
        second = tts.compute_key(right)
        self.assertEqual(first["cache_key"], second["cache_key"])
        self.assertEqual(first["asset_id"], second["asset_id"])
        self.assertRegex(first["cache_key"], r"^[a-f0-9]{64}$")
        self.assertEqual(first["asset_id"], "tts-" + first["cache_key"][:16])
        self.assertEqual(first["language"], "ZH")

    def test_cache_key_is_stable_across_calls(self):
        spec = make_spec()
        self.assertEqual(tts.compute_key(spec), tts.compute_key(spec))

    def test_cache_key_rejects_incomplete_or_unsafe_specs(self):
        with self.assertRaises(tts.TtsUsageError):
            tts.compute_key({"text": "x", "language": "zh"})
        with self.assertRaises(tts.TtsUsageError):
            tts.compute_key(make_spec(speaker="/abs/voice"))
        with self.assertRaises(tts.TtsUsageError):
            tts.compute_key(make_spec(speaker="付小春", voice_ref="a" * 64))


class PublishLookupTest(TtsBase):
    def test_cache_key_fields_differ(self):
        """验收矩阵 #2：voice/model/model_version/rate/sample_rate 各改一项 → 6 个键。"""
        variants = [
            make_spec(),
            make_spec(speaker="另一个音色"),
            make_spec(
                engine={"engine": "lstts", "model": "MeloTTS", "model_version": "unknown"}
            ),
            make_spec(
                engine={"engine": "lstts", "model": "IndexTTS", "model_version": "1.2.3"}
            ),
            make_spec(
                synthesis_params={
                    "rate": 1.2,
                    "pitch": None,
                    "style": None,
                    "emotion": None,
                    "speed": None,
                }
            ),
            make_spec(
                output={
                    "container": "wav",
                    "codec": "pcm_s16le",
                    "sample_rate": 24000,
                    "channels": 1,
                    "bit_depth": 16,
                }
            ),
        ]
        keys = {tts.compute_key(spec)["cache_key"] for spec in variants}
        self.assertEqual(len(keys), 6)
        ids = {tts.compute_key(spec)["asset_id"] for spec in variants}
        self.assertEqual(len(ids), 6)

    def test_publish_then_exact_lookup_hit_and_missing_param_no_hit(self):
        spec = make_spec()
        result, _staging, manifest = self.publish(spec)
        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["query_back"], "ok")
        self.assertEqual(manifest["asset_id"], result["asset_id"])

        hit = self.library.lookup(spec)
        self.assertTrue(hit["hit"])
        self.assertEqual(hit["match"], "exact")
        self.assertEqual(hit["asset_id"], result["asset_id"])
        self.assertEqual(hit["exit_code"], 0)
        self.assertEqual(hit["asset"]["audio"]["sha256"], manifest["audio"]["sha256"])

        changed = make_spec(
            synthesis_params={
                "rate": 1.1,
                "pitch": None,
                "style": None,
                "emotion": None,
                "speed": None,
            }
        )
        miss = self.library.lookup(changed)
        self.assertFalse(miss["hit"])
        self.assertIn(miss["match"], ("miss", "candidate"))
        self.assertEqual(miss["exit_code"], tts.util.EXIT_NO_SOURCE)

    def test_publish_twice_same_spec_is_idempotent(self):
        """验收矩阵 #1：同规格两个任务先后取件，只发布一次。"""
        spec = make_spec(text="幂等发布")
        first, staging, manifest = self.publish(spec, name="first")
        second, _same, _manifest = self.publish(
            spec, audio=audio_bytes(b"first"), name="second"
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["asset_id"], second["asset_id"])
        self.assertEqual(first["audio_sha256"], second["audio_sha256"])
        self.assertEqual(len(self.asset_dirs()), 1)
        self.assertEqual(len(os.listdir(staging)), 2)
        self.assertEqual(manifest["audio"]["sha256"], first["audio_sha256"])

    def test_publish_different_audio_same_key_becomes_v2(self):
        spec = make_spec(text="版本化发布")
        first, _staging, _manifest = self.publish(spec, audio=audio_bytes(b"A"), name="v1")
        second, _staging2, _manifest2 = self.publish(spec, audio=audio_bytes(b"B"), name="v2")
        self.assertTrue(first["created"])
        self.assertTrue(second["created"])
        self.assertEqual(second["asset_id"], first["asset_id"])
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["dir"], first["asset_id"] + "-v2")
        self.assertEqual(len(self.asset_dirs()), 2)
        newest = self.library.lookup(spec)
        self.assertTrue(newest["hit"])
        self.assertEqual(newest["asset"]["path"], second["dir"])

    def test_fetch_copies_and_does_not_mutate_library(self):
        """验收矩阵 #3：任务副本被处理，库原件 SHA 不变。"""
        spec = make_spec(text="取件复制")
        result, _staging, manifest = self.publish(spec)
        library_audio = os.path.join(
            self.root, result["dir"], manifest["audio"]["filename"]
        )
        library_sha = lib.sha256_file(library_audio)
        dest = os.path.join(self.tmp, "task-copy")
        fetched = self.library.fetch(result["asset_id"], dest)
        self.assertTrue(fetched["ok"])
        self.assertEqual(fetched["exit_code"], 0)
        copied_audio = fetched["dest"]["audio_path"]
        self.assertTrue(os.path.isfile(copied_audio))
        self.assertTrue(os.path.isfile(fetched["dest"]["manifest_path"]))
        self.assertEqual(lib.sha256_file(copied_audio), library_sha)
        self.assertNotEqual(
            os.stat(copied_audio).st_ino, os.stat(library_audio).st_ino
        )
        self.assertEqual(stat.S_IMODE(os.stat(copied_audio).st_mode) & 0o222, 0)

        os.chmod(copied_audio, 0o644)
        with open(copied_audio, "r+b") as handle:
            handle.seek(0)
            handle.write(b"DERIVED")
        self.assertNotEqual(lib.sha256_file(copied_audio), library_sha)
        self.assertEqual(lib.sha256_file(library_audio), library_sha)
        self.assertTrue(self.library.lookup(spec)["hit"])

    def test_fetch_rejects_destination_inside_library(self):
        spec = make_spec(text="越界取件")
        result, _staging, _manifest = self.publish(spec)
        with self.assertRaises(tts.TtsForbidden):
            self.library.fetch(result["asset_id"], os.path.join(self.root, "copy"))

    def test_concurrent_publish_single_canonical_asset(self):
        """验收矩阵 #4：并发同 key 发布只产生一个 canonical 资产目录。"""
        spec = make_spec(text="并发发布")
        payload = audio_bytes(b"C")
        stagings = [
            self.staging(spec, payload, name="concurrent-{0}".format(index))[0]
            for index in range(8)
        ]
        tasks = [self.task_dir("concurrent-{0}".format(index)) for index in range(8)]

        def worker(index):
            return self.library.publish(stagings[index], task_dir=tasks[index])

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(8)))
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual(sum(1 for item in results if item["created"]), 1)
        self.assertEqual(len(self.asset_dirs()), 1)
        self.assertEqual(len({item["audio_sha256"] for item in results}), 1)
        self.assertFalse([name for name in os.listdir(self.root) if name.startswith(".tts-")])


class OutboxTest(TtsBase):
    def test_publish_failure_writes_outbox_and_replay_is_idempotent(self):
        """验收矩阵 #5：发布中断 + outbox 重放，attempts 累加且不产生第二个资产。"""
        spec = make_spec(text="重放发布")
        staging, manifest = self.staging(spec, audio=audio_bytes(b"R"), name="replay")
        broken = json.loads(json.dumps(manifest))
        broken["audio"]["sha256"] = "f" * 64
        broken["audio"]["filename"] = "f" * 64 + ".wav"
        lib.write_json(os.path.join(staging, "manifest.json"), broken)
        task = self.task_dir()

        first = self.library.publish(staging, task_dir=task)
        self.assertFalse(first["ok"])
        self.assertEqual(first["reason"], "AUDIO_SHA256_MISMATCH")
        self.assertEqual(first["exit_code"], tts.util.EXIT_FAIL)
        self.assertTrue(os.path.isfile(first["outbox"]))
        pending = lib.read_json(first["outbox"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["attempts"], 1)
        self.assertEqual(self.asset_dirs(), [])

        lib.write_json(os.path.join(staging, "manifest.json"), manifest)
        replay = self.library.publish(staging, task_dir=task)
        self.assertTrue(replay["ok"])
        self.assertTrue(replay["created"])
        self.assertEqual(replay["outbox"], first["outbox"])
        resolved = lib.read_json(first["outbox"])
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["attempts"], 2)
        self.assertEqual(len(self.asset_dirs()), 1)

    def test_publish_unreachable_root_writes_outbox(self):
        """1006 不可达时发布不得本地伪造库，失败写 outbox 等重放。"""
        spec = make_spec(text="不可达发布")
        staging, _manifest = self.staging(spec, name="unreachable-publish")
        shutil.rmtree(self.root)
        result = self.library.publish(staging, task_dir=self.task_dir())
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], tts.util.EXIT_NO_SOURCE)
        self.assertEqual(result["reason"], "LIBRARY_UNWRITABLE")
        self.assertEqual(lib.read_json(result["outbox"])["status"], "pending")
        self.assertFalse(os.path.exists(self.root))

    def test_sensitive_publish_denied_exit_4(self):
        """验收矩阵 #8：shareable=false 发布 → 退出 4，outbox 记 SENSITIVE_DENIED。"""
        spec = make_spec(text="敏感文本")
        result, _staging, _manifest = self.publish(spec, shareable=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], tts.util.EXIT_FORBIDDEN)
        self.assertEqual(result["reason"], "SENSITIVE_DENIED")
        pending = lib.read_json(result["outbox"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["reason"], "SENSITIVE_DENIED")
        self.assertEqual(self.asset_dirs(), [])
        self.assertEqual(self.library.status()["counts"]["assets"], 0)

    def test_sensitivity_label_denied_even_when_shareable(self):
        spec = make_spec(text="带敏感标记")
        result, _staging, _manifest = self.publish(
            spec, shareable=True, sensitivity="secret"
        )
        self.assertEqual(result["exit_code"], tts.util.EXIT_FORBIDDEN)
        self.assertEqual(result["reason"], "SENSITIVE_DENIED")


class AclTest(TtsBase):
    def test_acl_project_and_node(self):
        """验收矩阵 #9：project 不匹配、restricted 越权拒绝；同项目/同节点可读。"""
        project_spec = make_spec(text="项目内资产")
        project_result, _staging, _manifest = self.publish(
            project_spec, scope={"visibility": "project", "projects": ["A"]}
        )
        self.assertTrue(project_result["ok"])

        restricted_spec = make_spec(text="受限资产")
        restricted_result, _staging2, _manifest2 = self.publish(
            restricted_spec,
            scope={"visibility": "restricted", "projects": [], "nodes": ["bszT14"]},
        )
        self.assertTrue(restricted_result["ok"])

        self.assertTrue(self.library.lookup(project_spec, project="A")["hit"])
        denied = self.library.lookup(project_spec, project="B")
        self.assertFalse(denied["hit"])
        self.assertEqual(denied["exit_code"], tts.util.EXIT_FORBIDDEN)
        self.assertEqual(denied["reason"], "ACL_DENIED")
        self.assertEqual(
            self.library.lookup(project_spec)["exit_code"], tts.util.EXIT_FORBIDDEN
        )

        self.assertTrue(self.library.lookup(restricted_spec, node="bszT14")["hit"])
        self.assertEqual(
            self.library.lookup(restricted_spec)["exit_code"], tts.util.EXIT_FORBIDDEN
        )
        fetched = self.library.fetch(
            project_result["asset_id"], os.path.join(self.tmp, "acl-copy"), project="B"
        )
        self.assertEqual(fetched["exit_code"], tts.util.EXIT_FORBIDDEN)


class CandidateStaleTest(TtsBase):
    def test_candidate_and_stale_policy(self):
        """验收矩阵 #10：candidate 默认拒绝，显式确认才可用；stale 不自动复用。"""
        spec = make_spec(text="候选资产")
        published, _staging, _manifest = self.publish(spec, quality="candidate", allow_candidate=True)
        self.assertTrue(published["ok"])

        default = self.library.lookup(spec)
        self.assertFalse(default["hit"])
        self.assertEqual(default["exit_code"], tts.util.EXIT_NO_SOURCE)
        self.assertEqual(default["reason"], "QUALITY_NOT_FORMAL")

        half = self.library.lookup(spec, allow_candidate=True)
        self.assertFalse(half["usable"])
        accepted = self.library.lookup(spec, allow_candidate=True, confirm=True)
        self.assertFalse(accepted["hit"])
        self.assertTrue(accepted["usable"])
        self.assertEqual(accepted["match"], "candidate")
        self.assertEqual(accepted["exit_code"], 0)

        manifest_path = os.path.join(self.root, published["dir"], "manifest.json")
        os.chmod(manifest_path, 0o644)
        stale = lib.read_json(manifest_path)
        stale["status"] = "stale"
        lib.write_json(manifest_path, stale)
        self.library.rebuild_index()

        blocked = self.library.lookup(spec)
        self.assertFalse(blocked["hit"])
        self.assertEqual(blocked["reason"], "STATUS_NOT_ACTIVE")
        inspected = self.library.lookup(spec, include_stale=True)
        self.assertFalse(inspected["hit"])
        self.assertFalse(inspected["usable"])
        self.assertEqual(inspected["match"], "stale")


class VerifyIndexTest(TtsBase):
    def test_corrupt_asset_rejected_quarantined_and_republishable(self):
        """验收矩阵 #6：损坏对象 lookup 拒绝，verify 隔离，重新合成后可发布。"""
        spec = make_spec(text="损坏隔离")
        published, staging, manifest = self.publish(spec)
        audio_path = os.path.join(self.root, published["dir"], manifest["audio"]["filename"])
        os.chmod(audio_path, 0o644)
        with open(audio_path, "rb") as handle:
            data = bytearray(handle.read())
        data[0] ^= 0xFF
        with open(audio_path, "wb") as handle:
            handle.write(bytes(data))

        rejected = self.library.lookup(spec)
        self.assertFalse(rejected["hit"])
        self.assertEqual(rejected["reason"], "SHA256_MISMATCH")
        self.assertEqual(rejected["exit_code"], tts.util.EXIT_FAIL)

        report = self.library.verify()
        self.assertFalse(report["ok"])
        self.assertEqual(report["corrupt"], 1)
        self.assertEqual(len(report["quarantined"]), 1)
        self.assertTrue(report["quarantined"][0].startswith("_quarantine/"))
        self.assertFalse(self.library.lookup(spec)["hit"])

        republished = self.library.publish(staging, task_dir=self.task_dir())
        self.assertTrue(republished["ok"])
        self.assertTrue(republished["created"])
        self.assertTrue(self.library.lookup(spec)["hit"])

    def test_index_rebuild_is_stable_and_has_controlled_paths(self):
        """验收矩阵 #11：索引稳定，条目只含受控相对路径。"""
        first, _staging, _manifest = self.publish(make_spec(text="索引一"), name="index-1")
        second, _staging2, _manifest2 = self.publish(make_spec(text="索引二"), name="index-2")
        document = self.library.index()
        self.assertEqual(document["schema"], tts.SCHEMA_INDEX)
        self.assertEqual(document["count"], 2)
        again = self.library.build_index()
        self.assertEqual(document["assets"], again["assets"])
        entry_ids = {entry["asset_id"] for entry in document["assets"]}
        self.assertEqual(entry_ids, {first["asset_id"], second["asset_id"]})
        for entry in document["assets"]:
            self.assertEqual(entry["path"], entry["dir"])
            self.assertFalse(os.path.isabs(entry["path"]))
            self.assertFalse(os.path.isabs(entry["audio"]["path"]))
            self.assertTrue(entry["audio"]["path"].startswith(entry["dir"] + "/"))
            self.assertRegex(entry["audio"]["sha256"], r"^[a-f0-9]{64}$")
        hit = self.library.lookup(make_spec(text="索引一"))
        self.assertTrue(hit["hit"])
        self.assertNotIn(self.root, json.dumps(hit["asset"], ensure_ascii=False))
        self.assertEqual(hit["asset"]["path"], hit["asset"]["path"].lstrip("/"))
        self.assertNotIn("normalized", json.dumps(hit["asset"], ensure_ascii=False))

    def test_index_policy_text_and_docs(self):
        """验收矩阵 #12：索引与文档明写「先查库、miss 才合成」，责任仓库 asset-tools。"""
        document = self.library.index()
        self.assertEqual(document["policy"]["rule"], "先查库、miss 才合成")
        self.assertEqual(document["policy"]["responsible_repo"], "asset-tools")
        doc_path = os.path.join(lib.REPO_ROOT, "docs", "tts-library.md")
        self.assertTrue(os.path.isfile(doc_path))
        with open(doc_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("先查库、miss 才合成", text)
        self.assertIn("asset-tools", text)
        self.assertGreaterEqual(len(text.splitlines()), 120)


class UnreachableTest(unittest.TestCase):
    def test_unreachable_root_exit_3(self):
        """验收矩阵 #7：库不可达 → 退出 3 + unreachable=true，绝不输出「不存在」。"""
        with lib.temp_dir("tts-unreachable-") as tmp:
            missing = os.path.join(tmp, "no-such-library")
            library = tts.TtsLibrary(missing)
            status = library.status()
            self.assertFalse(status["reachable"])
            self.assertEqual(status["status"], "UNREACHABLE")
            self.assertEqual(status["exit_code"], tts.util.EXIT_NO_SOURCE)
            result = library.lookup(make_spec())
            self.assertTrue(result["unreachable"])
            self.assertEqual(result["exit_code"], tts.util.EXIT_NO_SOURCE)
            self.assertIn("UNREACHABLE", result["message"])
            self.assertNotIn("不存在", json.dumps(result, ensure_ascii=False))
            fetched = library.fetch("tts-0123456789abcdef", os.path.join(tmp, "dest"))
            self.assertTrue(fetched["unreachable"])
            self.assertEqual(fetched["exit_code"], tts.util.EXIT_NO_SOURCE)

            spec_path = os.path.join(tmp, "spec.json")
            lib.write_json(spec_path, make_spec())
            code, stdout, _stderr = call_cli(
                ["tts-library", "status", "--root", missing, "--json"]
            )
            self.assertEqual(code, tts.util.EXIT_NO_SOURCE)
            self.assertTrue(json.loads(stdout)["unreachable"])
            code, stdout, _stderr = call_cli(["tts-library", "status", "--root", missing])
            self.assertEqual(code, tts.util.EXIT_NO_SOURCE)
            self.assertIn("UNREACHABLE", stdout)
            self.assertNotIn("不存在", stdout)


class CliTest(TtsBase):
    def test_cli_key_lookup_fetch_publish(self):
        spec = make_spec(text="命令行闭环")
        spec_path = os.path.join(self.tmp, "spec.json")
        lib.write_json(spec_path, spec)

        code, stdout, _stderr = call_cli(
            ["tts-library", "key", "--root", self.root, "--spec", spec_path, "--json"]
        )
        self.assertEqual(code, 0)
        key_payload = json.loads(stdout)
        self.assertEqual(key_payload["asset_id"], tts.compute_key(spec)["asset_id"])

        staging, _manifest = self.staging(spec, name="cli")
        task = self.task_dir()
        code, stdout, _stderr = call_cli(
            [
                "tts-library",
                "publish",
                "--root",
                self.root,
                "--staging",
                staging,
                "--task-dir",
                task,
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["created"])

        code, stdout, _stderr = call_cli(
            ["tts-library", "lookup", "--root", self.root, "--spec", spec_path, "--json"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["hit"])

        code, stdout, _stderr = call_cli(
            [
                "tts-library",
                "fetch",
                "--root",
                self.root,
                "--spec",
                spec_path,
                "--task-dir",
                task,
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        fetched = json.loads(stdout)
        self.assertTrue(fetched["ok"])
        self.assertEqual(
            fetched["dest"]["dir"],
            os.path.join(task, "assets", "tts", "original", key_payload["asset_id"]),
        )

    def test_cli_publish_requires_task_dir(self):
        spec = make_spec(text="缺任务目录")
        staging, _manifest = self.staging(spec, name="no-task")
        code, stdout, _stderr = call_cli(
            [
                "tts-library",
                "publish",
                "--root",
                self.root,
                "--staging",
                staging,
                "--json",
            ]
        )
        self.assertEqual(code, tts.util.EXIT_USAGE)
        self.assertEqual(json.loads(stdout)["reason"], "USAGE")


if __name__ == "__main__":
    unittest.main()
