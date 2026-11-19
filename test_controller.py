"""Run isolated tests with python3 -B -m unittest -v."""
import json
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from controller import dry_run, frame, parse, playback


class ControllerTests(unittest.TestCase):
    def test_cli_recording_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sequence.json"
            path.write_text('{"frames":[{"at_ms":0,"axes":{"ry":1},"buttons":["a"]},'
                            '{"at_ms":120,"axes":{"ry":-1}},{"at_ms":180}]}')
            result = subprocess.run(["./agentpad", "play", path], capture_output=True, check=True)
            self.assertEqual(dry_run(path.read_bytes()), result.stdout)
            self.assertEqual(json.loads(result.stdout)["frames"], [
                frame(0, {"ry": 1}, ["a"]), frame(120, {"ry": -1}), frame(180),
            ])
            path.write_bytes(result.stdout)
            replay = subprocess.check_output(["./agentpad", "play", path])
            self.assertEqual(replay, result.stdout)
            path.write_text('{"frames":[{"at_ms":0},{"at_ms":0}]}')
            result = subprocess.run(["./agentpad", "play", path], capture_output=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")
            self.assertIn(b"at_ms must increase", result.stderr)

    def test_dry_run_counts_its_final_newline(self):
        source = b'{"frames":[{"at_ms":0}]}'
        normalized = dry_run(source)
        self.assertTrue(normalized.endswith(b"\n"))
        with patch("controller.MAX_BYTES", len(normalized)):
            self.assertEqual(dry_run(source), normalized)
        with patch("controller.MAX_BYTES", len(normalized) - 1):
            self.assertEqual(dry_run(source), source)
            self.assertEqual(dry_run(dry_run(source)), source)

    def test_release_failure_preserves_original_playback_error(self):
        def emit(snapshot):
            if snapshot["buttons"]:
                raise OSError("frame write failed")
            raise RuntimeError("release failed")

        with self.assertRaisesRegex(OSError, "frame write failed") as error:
            playback([frame(0, buttons=["a"])], emit)
        self.assertIsInstance(error.exception.__cause__, RuntimeError)

    def test_invalid_snapshots(self):
        for data in (
            b'{"frames":[]}', b'{"frames":[{"at_ms":false}]}',
            b'{"frames":[{"at_ms":0,"axes":{"ry":NaN}}]}',
            b'{"frames":[{"at_ms":0,"axes":{"lt":-0.1}}]}',
            b'{"frames":[{"at_ms":0,"axes":{"ry":true}}]}',
            b'{"frames":[{"at_ms":0,"buttons":["A"]}]}',
            b'{"frames":[{"at_ms":0,"buttons":[[]]}]}',
            b'{"frames":[{"at_ms":0,"at_ms":1}]}',
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse(data)

    def test_emit_failure_and_interrupt_attempt_release(self):
        active = frame(0, {"ry": 1}, ["a"])
        for error in (OSError("write failed"), KeyboardInterrupt()):
            emit = Mock(side_effect=[error, None])
            with self.subTest(error=error), self.assertRaises(type(error)):
                playback([active], emit)
            self.assertEqual(emit.call_args_list, [call(active), call(frame(0))])

    def test_disconnect_cancels_wait_and_releases(self):
        peer, client = socket.socketpair()
        with peer:
            client.close()
            seen = []
            with self.assertRaisesRegex(ValueError, "client disconnected"):
                playback([frame(600_000, {"ry": 1})], seen.append, peer)
            self.assertEqual(seen, [frame(0)])
