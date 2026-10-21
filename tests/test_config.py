"""Explicit configuration and the existing library/provider boundary, entirely offline."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conric import SchemaError, load
import transcribe
from transcribe import cli
from transcribe.config import ConfigurationError, validate_config
from test_transcribe_cli import sample_transcript


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "audio.mp3"
        self.source.write_bytes(b"synthetic")

    def config(self, text, name="settings.yaml"):
        file = self.root / name
        file.write_text(text, encoding="utf-8")
        return file

    def args(self, file, *options):
        return cli.parse_args([str(self.source), "--config", str(file), *options])

    def test_schema_is_reusable_at_a_parent_subtree_and_retains_provenance(self):
        file = self.config("audio:\n  transcription:\n    model: scribe_v2\n")
        document = load([file])
        settings = document.validate(validate_config, path=("audio",))
        self.assertEqual(dict(settings.overrides), {"model": "scribe_v2"})
        self.assertEqual(document.source(("audio", "transcription", "model")).line, 3)
        with self.assertRaises(AttributeError):
            settings.algorithms = ()

    def test_includes_and_explicit_overlays_then_only_explicit_cli_override(self):
        self.config("transcription:\n  language: eng\n  model: base\ncleanup:\n  mode: on\n", "base.yaml")
        first = self.config("include: [base.yaml]\ntranscription:\n  model: first\n")
        second = self.config("transcription:\n  model: last\ncleanup:\n  mode: auto\n", "overlay.yaml")
        args = self.args(first, "--config", str(second))
        self.assertEqual((args.model, args.language_code, args.leveling), ("last", "eng", "auto"))
        args = self.args(first, "--config", str(second), "--model=explicit", "--leveling", "off")
        self.assertEqual((args.model, args.language_code, args.leveling), ("explicit", "eng", "off"))

    def test_false_zero_and_null_are_not_missing_values(self):
        args = self.args(self.config("uneven_threshold_db: 0\ntranscription:\n  language: null\n  audio_events: false\n  clean_transcript: false\nsecond_pass:\n  mode: off\n"))
        self.assertEqual(args.uneven_threshold, 0)
        self.assertIsNone(args.language_code)
        self.assertIs(args.audio_events, False)
        self.assertIs(args.clean_transcript, False)
        self.assertEqual(args.second_pass, "off")

    def test_large_integers_do_not_escape_the_schema_error_contract(self):
        large = 10 ** 1000
        self.assertEqual(dict(validate_config({"uneven_threshold_db": large}).overrides),
                         {"uneven_threshold": large})
        with self.assertRaises(SchemaError):
            validate_config({"transcription": {"diarization": {"threshold": large}}})

    def test_explicit_diarization_selection_replaces_yaml_alternative(self):
        count = self.config("transcription:\n  diarization:\n    max_speakers: 3\n")
        args = self.args(count, "--diarization-threshold", "0.3")
        self.assertIsNone(args.max_speakers)
        self.assertEqual(cli.resolved_settings(args)[1], 0.3)
        threshold = self.config("transcription:\n  diarization:\n    threshold: 0.3\n")
        args = self.args(threshold, "--max-speakers", "2")
        self.assertIsNone(cli.resolved_settings(args)[1])
        self.assertEqual(args.max_speakers, 2)

    def test_yaml_algorithms_use_all_off_baseline_then_existing_cli_overrides(self):
        file = self.config("cleanup:\n  algorithms:\n    leveler: true\n    levelerstrength_speech: 0\n")
        args = self.args(file, "--auphonic-set", "leveler=false", "--speech-leveler-strength", "100")
        algorithms, _ = cli.resolved_settings(args)
        self.assertFalse(algorithms["leveler"])
        self.assertFalse(algorithms["denoise"])
        self.assertEqual(algorithms["levelerstrength_speech"], 100)
        with self.assertRaises(ConfigurationError):
            self.args(file, "--auphonic-config", "other.json")
        empty = self.args(self.config("cleanup:\n  algorithms: {}\n"))
        self.assertEqual(cli.resolved_settings(empty)[0], cli.auphonic.ALL_PROCESSING_OFF)

    def test_legacy_json_and_omitted_configuration_are_unchanged(self):
        self.config("cleanup:\n  mode: on\n")  # No ambient discovery.
        args = cli.parse_args([str(self.source)])
        self.assertEqual((args.leveling, args.second_pass, args.model), ("off", "auto", "scribe_v2"))
        self.assertEqual(cli.resolved_settings(args)[0], json.loads(cli.DEFAULT_AUPHONIC_CONFIG.read_text()))
        preset = self.root / "preset.json"
        preset.write_text('{"leveler": true}')
        args = cli.parse_args([str(self.source), "--auphonic-config", str(preset)])
        self.assertEqual(cli.resolved_settings(args)[0], {**cli.auphonic.ALL_PROCESSING_OFF, "leveler": True})

    def test_invalid_schema_rejects_types_ranges_unknown_fields_and_authority(self):
        invalid = [
            "transcription: []", "transcription: null", "unknown: value",
            "transcription: {provider: other}", "cleanup: {provider: other}",
            "transcription: {endpoint: 'https://example.invalid'}", "credentials: {}",
            "transcription: {model: !env CONFIG_TEST_SECRET}", "transcription: {model: ''}",
            "transcription: {audio_events: 1}", "transcription: {language: false}",
            "transcription: {diarization: {max_speakers: true}}",
            "transcription: {diarization: {max_speakers: 33}}",
            "transcription: {diarization: {threshold: 0.5}}",
            "transcription: {diarization: {threshold: 0.2, max_speakers: 2}}",
            "transcription: {speaker_roles: true, diarization: {max_speakers: 3}}",
            "cleanup: {mode: never}", "cleanup: {bitrate_kbps: 31}",
            "cleanup: {bitrate_kbps: 128.0}", "cleanup: {algorithms: {leveler: 1}}",
            "cleanup: {algorithms: {unsupported: true}}", "second_pass: {mode: false}",
            "uneven_threshold_db: -1", "local_only: false",
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ConfigurationError):
                self.args(self.config(text))

    def test_unsafe_or_missing_inputs_fail_without_scalar_disclosure(self):
        secret = "synthetic-sensitive-scalar"
        for text in ["transcription: [", "include: [missing.yaml]", "include: [../outside.yaml]",
                     "transcription: &x {model: a}\ncleanup: *x", "x: 1\nx: 2",
                     f"transcription: {{api_key: {secret}}}", f"{secret}: true"]:
            with self.subTest(text=text), redirect_stderr(io.StringIO()) as stderr:
                file = self.config(text)
                with patch.object(cli.elevenlabs, "load_api_key", side_effect=AssertionError("credential access")):
                    self.assertEqual(cli.main([str(self.source), "--config", str(file)]), 1)
                self.assertNotIn(secret, stderr.getvalue())
                self.assertFalse(self.source.with_suffix("").exists())
        with self.assertRaises(ConfigurationError):
            self.args(self.root / "absent.yaml")

    def test_dry_run_does_not_load_credentials_construct_clients_or_write(self):
        file = self.config("cleanup: {mode: on}\ntranscription: {provider: elevenlabs}\n")
        before = set(self.root.iterdir())
        with (patch.object(cli.auphonic, "load_api_key", side_effect=AssertionError("key")),
              patch.object(cli.elevenlabs, "load_api_key", side_effect=AssertionError("key")),
              patch.object(cli.auphonic, "AuphonicClient", side_effect=AssertionError("client")),
              patch.object(cli.elevenlabs, "ElevenLabsClient", side_effect=AssertionError("client")),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(cli.main([str(self.source), "--config", str(file), "--dry-run"]), 0)
        self.assertEqual(set(self.root.iterdir()), before)

    def test_per_call_config_and_explicit_credentials_remain_isolated(self):
        before = dict(os.environ)

        def run(index):
            file = self.config(f"transcription: {{model: selected-{index}}}\nsecond_pass: {{mode: off}}\n",
                               f"call-{index}.yaml")
            source = self.root / f"call-{index}.mp3"
            source.write_bytes(b"synthetic")
            client = Mock()
            client.transcribe.return_value = sample_transcript()
            package = transcribe.transcribe_file(source, "--config", str(file), elevenlabs_client=client)
            self.assertEqual(package.sidecar["settings"]["model_id"], f"selected-{index}")
            return package

        with (patch.object(cli.elevenlabs, "load_api_key", side_effect=AssertionError("ambient")),
              patch.object(cli.auphonic, "load_api_key", side_effect=AssertionError("unused"))):
            with ThreadPoolExecutor(max_workers=2) as pool:
                packages = list(pool.map(run, [0, 1]))
        self.assertEqual(len(packages), 2)
        self.assertEqual(dict(os.environ), before)

    def test_explicit_key_with_yaml_bypasses_ambient_lookup_and_is_not_recorded(self):
        file = self.config("second_pass: {mode: off}\n")
        client = Mock()
        client.transcribe.return_value = sample_transcript()
        with (patch.object(cli.elevenlabs, "load_api_key", side_effect=AssertionError("ambient")),
              patch.object(cli.auphonic, "load_api_key", side_effect=AssertionError("unused")),
              patch.object(cli.elevenlabs, "ElevenLabsClient", return_value=client) as constructor):
            package = transcribe.transcribe_file(self.source, "--config", str(file),
                                                 elevenlabs_api_key="synthetic-secret-key")
        constructor.assert_called_once_with("synthetic-secret-key")
        self.assertNotIn("synthetic-secret-key", json.dumps(package.sidecar))

    def test_final_cli_yaml_combination_is_validated_before_writes_or_clients(self):
        file = self.config("transcription: {speaker_roles: true}\n")
        with (patch.object(cli.elevenlabs, "ElevenLabsClient", side_effect=AssertionError("client")),
              self.assertRaises(cli.PipelineError)):
            cli.run_pipeline(self.args(file, "--max-speakers", "3"))
        self.assertFalse(self.source.with_suffix("").exists())


if __name__ == "__main__":
    unittest.main()
