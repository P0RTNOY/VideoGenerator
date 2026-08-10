import hashlib
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError

from editing_audio_assets import (
    EditingAudioAsset,
    EditingAudioAssetError,
    EditingAudioManifest,
    EditingAudioManifestError,
    add_editing_audio_asset,
    editing_audio_manifest_path,
    ingest_editing_audio_asset,
    load_editing_audio_manifest,
    read_verified_editing_audio_bytes,
    resolve_verified_editing_audio_asset,
    save_editing_audio_manifest,
)


def make_audio(path: Path, *, codec: str, duration=2.0) -> bytes:
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={duration}",
        "-ac",
        "2",
    ]
    if codec == "mp3":
        command.extend(["-c:a", "libmp3lame", "-b:a", "128k"])
    elif codec == "wav":
        command.extend(["-c:a", "pcm_s16le"])
    else:
        raise AssertionError(f"unsupported test codec: {codec}")
    command.extend(["-y", str(path)])
    subprocess.run(command, check=True, capture_output=True)
    return path.read_bytes()


class EditingAudioAssetTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.storage_root = self.root / "products"
        self.mp3_path = self.root / "music.mp3"
        self.wav_path = self.root / "effect.wav"
        self.mp3_bytes = make_audio(self.mp3_path, codec="mp3")
        self.wav_bytes = make_audio(self.wav_path, codec="wav")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def ingest(self, source_path=None, *, kind="music", label="manual:test"):
        return ingest_editing_audio_asset(
            product_id="example-product",
            kind=kind,
            source_path=source_path or self.mp3_path,
            source_label=label,
            storage_root=self.storage_root,
        )

    def test_valid_mp3_ingestion_uses_content_identity_and_real_metadata(self):
        original = hashlib.sha256(self.mp3_bytes).hexdigest()
        asset = self.ingest()

        self.assertEqual(asset.id, f"editing-audio:music:{original}")
        self.assertEqual(asset.sha256, original)
        self.assertEqual(asset.kind, "music")
        self.assertEqual(asset.byte_size, len(self.mp3_bytes))
        self.assertEqual(asset.container, "mp3")
        self.assertEqual(asset.codec, "mp3")
        self.assertGreater(asset.duration_ms, 0)
        self.assertEqual(asset.channels, 2)
        self.assertEqual(asset.sample_rate_hz, 48000)
        self.assertEqual(asset.source_label, "manual:test")
        self.assertEqual(Path(asset.local_path).read_bytes(), self.mp3_bytes)

    def test_valid_wav_ingestion_captures_exact_duration_and_sfx_kind(self):
        asset = self.ingest(self.wav_path, kind="sfx", label="manual:effect")

        self.assertEqual(asset.id, f"editing-audio:sfx:{asset.sha256}")
        self.assertEqual(asset.container, "wav")
        self.assertEqual(asset.codec, "pcm_s16le")
        self.assertEqual(asset.duration_ms, 2000)
        self.assertEqual(asset.channels, 2)
        self.assertEqual(asset.sample_rate_hz, 48000)

    def test_non_audio_corrupt_empty_and_unsupported_formats_are_rejected(self):
        video = self.root / "video.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=90x160:d=1",
                "-an",
                "-c:v",
                "libx264",
                "-y",
                str(video),
            ],
            check=True,
            capture_output=True,
        )
        corrupt = self.root / "corrupt.mp3"
        corrupt.write_bytes(b"not audio")
        empty = self.root / "empty.wav"
        empty.write_bytes(b"")
        m4a = self.root / "unsupported.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "aac",
                "-y",
                str(m4a),
            ],
            check=True,
            capture_output=True,
        )

        for source in (video, corrupt, empty, m4a):
            with self.subTest(source=source.name):
                with self.assertRaises(EditingAudioAssetError):
                    self.ingest(source)

    def test_identity_ignores_filename_and_duplicate_bytes_reuse_storage(self):
        renamed = self.root / "completely-different-name.mp3"
        renamed.write_bytes(self.mp3_bytes)

        first = self.ingest()
        second = self.ingest(renamed)

        self.assertEqual(first, second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.local_path, second.local_path)
        self.assertEqual(
            Path(first.local_path),
            (
                self.storage_root
                / "example-product"
                / "assets"
                / "editing"
                / "audio"
                / f"{first.sha256}.mp3"
            ).resolve(),
        )

    def test_same_bytes_can_be_reused_under_distinct_explicit_kinds(self):
        music = self.ingest(kind="music")
        sfx = self.ingest(kind="sfx")

        self.assertNotEqual(music.id, sfx.id)
        self.assertEqual(music.sha256, sfx.sha256)
        self.assertEqual(music.local_path, sfx.local_path)

    def test_ingestion_does_not_modify_the_caller_source(self):
        before = self.mp3_path.read_bytes()
        stat_before = self.mp3_path.stat()

        self.ingest()

        self.assertEqual(self.mp3_path.read_bytes(), before)
        self.assertEqual(self.mp3_path.stat().st_size, stat_before.st_size)

    def test_invalid_logical_metadata_fails_before_content_storage(self):
        unused_storage = self.root / "invalid-products"

        with self.assertRaises(EditingAudioAssetError):
            ingest_editing_audio_asset(
                product_id="example-product",
                kind="music",
                source_path=self.mp3_path,
                source_label="   ",
                storage_root=unused_storage,
            )

        self.assertFalse(unused_storage.exists())

    def test_changed_stored_bytes_or_metadata_are_detected_at_consumer_time(self):
        asset = self.ingest()
        self.assertEqual(read_verified_editing_audio_bytes(asset), self.mp3_bytes)

        Path(asset.local_path).write_bytes(b"changed")
        with self.assertRaises(EditingAudioAssetError):
            read_verified_editing_audio_bytes(asset)

        Path(asset.local_path).write_bytes(self.mp3_bytes)
        bypassed = asset.model_copy(update={"sample_rate_hz": 44100}, deep=True)
        with self.assertRaises(EditingAudioAssetError):
            read_verified_editing_audio_bytes(bypassed)

    def test_model_is_strict_and_has_no_rights_network_or_voiceover_state(self):
        asset = self.ingest()
        forbidden = {
            "license",
            "rights",
            "commercial_use",
            "source_url",
            "download_url",
            "voice_over",
        }
        self.assertTrue(forbidden.isdisjoint(EditingAudioAsset.model_fields))
        data = asset.model_dump()
        data["license"] = "assumed"
        with self.assertRaises(ValidationError):
            EditingAudioAsset.model_validate(data)
        with self.assertRaises(ValidationError):
            EditingAudioAsset.model_validate(
                {**asset.model_dump(), "kind": "voice_over"}
            )


class EditingAudioManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.storage_root = self.root / "products"
        self.source = self.root / "music.wav"
        make_audio(self.source, codec="wav")
        self.asset = ingest_editing_audio_asset(
            product_id="example-product",
            kind="music",
            source_path=self.source,
            source_label="manual:fixture",
            storage_root=self.storage_root,
        )
        self.manifest = EditingAudioManifest(product_id="example-product")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_manifest_add_is_immutable_and_resolves_verified_current_asset(self):
        before_manifest = deepcopy(self.manifest.model_dump())
        before_asset = deepcopy(self.asset.model_dump())

        updated = add_editing_audio_asset(self.manifest, self.asset)
        resolved = resolve_verified_editing_audio_asset(
            updated,
            self.asset.id,
            expected_kind="music",
        )

        self.assertEqual(resolved, self.asset)
        self.assertEqual(self.manifest.model_dump(), before_manifest)
        self.assertEqual(self.asset.model_dump(), before_asset)
        self.assertEqual(updated.assets, (self.asset,))

    def test_manifest_rejects_wrong_product_duplicate_id_and_kind_mismatch(self):
        wrong_product = self.asset.model_copy(
            update={"product_id": "other-product"}, deep=True
        )
        with self.assertRaises(EditingAudioManifestError):
            add_editing_audio_asset(self.manifest, wrong_product)

        updated = add_editing_audio_asset(self.manifest, self.asset)
        with self.assertRaises(EditingAudioManifestError):
            add_editing_audio_asset(updated, self.asset)
        with self.assertRaises(EditingAudioManifestError):
            resolve_verified_editing_audio_asset(
                updated,
                self.asset.id,
                expected_kind="sfx",
            )

    def test_manifest_json_round_trip_is_human_readable_and_product_scoped(self):
        updated = add_editing_audio_asset(self.manifest, self.asset)
        path = save_editing_audio_manifest(updated, self.storage_root)
        raw = path.read_text(encoding="utf-8")
        loaded = load_editing_audio_manifest(
            "example-product",
            self.storage_root,
        )

        self.assertEqual(
            path,
            editing_audio_manifest_path(updated, self.storage_root).resolve(),
        )
        self.assertEqual(
            path,
            (
                self.storage_root
                / "example-product"
                / "assets"
                / "editing"
                / "audio"
                / "manifest.json"
            ).resolve(),
        )
        self.assertEqual(loaded, updated)
        self.assertTrue(raw.endswith("\n"))
        self.assertIn("\n  \"schema_version\"", raw)

    def test_missing_manifest_loads_empty_and_unknown_fields_are_rejected(self):
        loaded = load_editing_audio_manifest(
            "example-product",
            self.storage_root,
        )
        self.assertEqual(loaded, self.manifest)

        data = self.manifest.model_dump()
        data["license_policy"] = "unknown"
        with self.assertRaises(ValidationError):
            EditingAudioManifest.model_validate(data)


if __name__ == "__main__":
    unittest.main()
