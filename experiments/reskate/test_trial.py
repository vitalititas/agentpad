"""Offline tests for the ReSkate trial archive and dry-run CLI."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import trial

HERE = Path(__file__).resolve().parent
AGENTPAD = HERE.parents[1] / "agentpad"
NEUTRAL = b'{ "frames": [ { "at_ms": 0 } ] }\r\n'
EXAMPLES = ("neutral", "stick-calibration", "ollie-120", "ollie-240")


class TrialTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "recorded"
        self.source = self.root / "source.json"
        self.source.write_bytes(NEUTRAL)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-B", HERE / "trial.py", *args],
            cwd=self.root,
            check=False,
            capture_output=True,
            timeout=5,
        )

    def record(self):
        trial.record(self.source, self.archive, "unknown", "Offline")

    def assert_rejected(self, process):
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(process.stdout, b"")
        self.assertNotIn(b"Traceback", process.stderr)

    def test_record_preserves_exact_bytes_and_metadata(self):
        result = self.cli(
            "record", self.source, self.archive, "unknown", "--note", "Offline"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.archive / "inputs.json").read_bytes(), NEUTRAL)
        self.assertEqual(
            json.loads((self.archive / "observation.json").read_bytes()),
            {
                "sha256": hashlib.sha256(NEUTRAL).hexdigest(),
                "result": "unknown",
                "note": "Offline",
            },
        )

        (self.archive / "inputs.json").write_bytes(NEUTRAL + b" ")
        result = self.cli("replay", self.archive)
        self.assert_rejected(result)
        self.assertIn(b"hash mismatch", result.stderr)

    def test_examples_replay_matches_agentpad_stdout(self):
        for name in EXAMPLES:
            with self.subTest(example=name):
                example = HERE / f"{name}.json"
                frames = json.loads(example.read_bytes())["frames"]
                self.assertEqual(frames[0], {"at_ms": 0})
                self.assertEqual(frames[-1], {"at_ms": frames[-1]["at_ms"]})
                for frame in frames:
                    self.assertLessEqual(set(frame.get("axes", {})), {"rx", "ry"})
                    self.assertFalse(frame.get("buttons"))
                recorded = self.cli("record", example, self.root / name, "unknown")
                self.assertEqual(recorded.returncode, 0, recorded.stderr)
                expected = subprocess.run(
                    [AGENTPAD, "play", example],
                    cwd=self.root,
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
                replayed = self.cli("replay", self.root / name)
                self.assertEqual(replayed.returncode, 0, replayed.stderr)
                self.assertEqual(replayed.stdout, expected.stdout)

    def test_replay_uses_the_bytes_already_checked(self):
        self.record()
        with (
            patch.object(trial, "read_regular", wraps=trial.read_regular) as read,
            patch.object(
                trial, "validate_sequence", wraps=trial.validate_sequence
            ) as validate,
            patch.object(
                trial.controller, "dry_run", create=True, return_value=b"out"
            ) as dry,
        ):
            self.assertEqual(trial.replay(self.archive), b"out")
        self.assertEqual(
            [call.args[0] for call in read.call_args_list],
            [
                self.archive / "observation.json",
                self.archive / "inputs.json",
            ],
        )
        validate.assert_called_once_with(NEUTRAL)
        dry.assert_called_once_with(NEUTRAL)
        self.assertIs(dry.call_args.args[0], validate.call_args.args[0])

    def test_record_and_replay_reject_invalid_sequences_even_with_matching_hash(self):
        self.record()
        for payload in (
            b'{"frames":[{"at_ms":0,"axes":{"ry":2}},{"at_ms":10}]}',
            b'{"frames":[{"at_ms":0,"buttons":["other"]},{"at_ms":10}]}',
            b'{"frames":[{"at_ms":0,"at_ms":1},{"at_ms":10}]}',
            b'{"frames":[{"at_ms":10},{"at_ms":0}]}',
            b'{"frames":[{"at_ms":0,"buttons":["a"]}]}',
            b'{"frames":[{"at_ms":0,"axes":{"ry":1}}]}',
            b'{"frames":[]}',
            b"invalid",
            b"\xff",
            b"[" * 1500 + b"]" * 1500,
        ):
            with self.subTest(payload=payload[:80]):
                self.source.write_bytes(payload)
                rejected = self.root / "rejected"
                self.assert_rejected(
                    self.cli("record", self.source, rejected, "unknown")
                )
                self.assertFalse(rejected.exists())
                (self.archive / "inputs.json").write_bytes(payload)
                (self.archive / "observation.json").write_text(
                    json.dumps(
                        {
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "result": "miss",
                            "note": "",
                        }
                    )
                )
                self.assert_rejected(self.cli("replay", self.archive))

    def test_bounded_reads_include_the_exact_limit(self):
        limit = trial.controller.MAX_BYTES
        self.source.write_bytes(NEUTRAL.ljust(limit, b" "))
        self.record()
        self.source.write_bytes(NEUTRAL.ljust(limit + 1, b" "))
        self.assert_rejected(
            self.cli("record", self.source, self.root / "oversize", "unknown")
        )
        self.assertFalse((self.root / "oversize").exists())
        for name in ("inputs.json", "observation.json"):
            with self.subTest(member=name):
                path = self.archive / name
                original = path.read_bytes()
                path.write_bytes(original.ljust(limit + 1, b" "))
                result = self.cli("replay", self.archive)
                self.assert_rejected(result)
                self.assertIn(b"exceeds", result.stderr)
                path.write_bytes(original)

    def test_regular_file_requirement(self):
        self.record()
        for path in (
            self.source,
            self.archive / "inputs.json",
            self.archive / "observation.json",
        ):
            args = (
                ("record", self.source, self.root / "new", "unknown")
                if path == self.source
                else ("replay", self.archive)
            )
            with self.subTest(path=path):
                original = path.read_bytes()
                path.unlink()
                os.mkfifo(path)
                self.assert_rejected(self.cli(*args))
                path.unlink()
                target = self.root / "target"
                target.write_bytes(original)
                path.symlink_to(target)
                self.assert_rejected(self.cli(*args))
                path.unlink()
                path.write_bytes(original)

    def test_invalid_observations(self):
        self.record()
        path = self.archive / "observation.json"
        original = json.loads(path.read_bytes())
        for metadata in (
            None,
            {},
            original | {"note": []},
            original | {"result": "other"},
        ):
            with self.subTest(metadata=metadata):
                path.write_text(json.dumps(metadata))
                self.assert_rejected(self.cli("replay", self.archive))

    def test_duplicate_preserves_empty_directory_and_complete_archive(self):
        self.archive.mkdir()
        self.assert_rejected(self.cli("record", self.source, self.archive, "miss"))
        self.assertEqual(list(self.archive.iterdir()), [])
        self.archive.rmdir()
        self.record()
        contents = {p.name: p.read_bytes() for p in self.archive.iterdir()}
        self.source.write_bytes(NEUTRAL + b" ")
        self.assert_rejected(self.cli("record", self.source, self.archive, "miss"))
        self.assertEqual(
            {p.name: p.read_bytes() for p in self.archive.iterdir()}, contents
        )
        self.assertEqual(set(self.root.iterdir()), {self.source, self.archive})

    def test_staging_cleaned_on_write_failure(self):
        write = Path.write_bytes

        def fail_observation(path, data):
            if path.name == "observation.json":
                self.assertFalse(self.archive.exists())
                self.assertEqual((path.parent / "inputs.json").read_bytes(), NEUTRAL)
                raise OSError("simulated write failure")
            return write(path, data)

        with (
            patch.object(Path, "write_bytes", fail_observation),
            self.assertRaisesRegex(OSError, "simulated write failure"),
        ):
            self.record()
        self.assertEqual(set(self.root.iterdir()), {self.source})


if __name__ == "__main__":
    unittest.main()
