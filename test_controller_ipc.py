"""Local protocol and lifecycle checks with an in-memory controller backend."""
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import controller as pad

SEQUENCE = b'{\n  "frames": [{"at_ms": 0, "buttons":["a"]}, {"at_ms": 1}]\n}\n'


def run_fake_server(fast):
    # Exercise the production CLI, listener, playback loop and signal cleanup.
    # Skip only the device and the per-frame wait so long recordings run quickly.
    system_select = select.select
    def select_now(read, write, errors, timeout):
        return system_select(read, write, errors, 0 if fast else timeout)

    with patch.object(pad, "open_pad", return_value=nullcontext(lambda snapshot: None)), \
            patch.object(select, "select", select_now), \
            patch.object(pad, "IO_TIMEOUT", 0.5):
        sys.exit(pad.main())


@contextmanager
def fake_server(path, fast=False):
    command = [sys.executable, "-B", "-c",
               f"from test_controller_ipc import run_fake_server; run_fake_server({fast!r})",
               "serve", "--real", "--socket", path]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        try:
            if not select.select([child.stdout], [], [], 5)[0]:
                raise AssertionError("server readiness timed out")
            if child.stdout.readline() != os.fsencode(f"ready {path}\n"):
                raise AssertionError("server exited before readiness")
            yield
        finally:
            child.terminate()
            try:
                _, stderr = child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise
            if child.returncode != 143:
                raise AssertionError(f"server exited {child.returncode}: {stderr!r}")


class SocketIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.path = self.directory / "pad.sock"

    def test_limit_size_cli_record_replay_and_play(self):
        # Expansion and the old sparse wire encoding would both exceed 1 MiB.
        frames = b",".join(b'{"at_ms":%d}' % offset for offset in range(30000))
        source = (b'{\n"frames":[\n' + frames + b'\n]\n}').ljust(pad.MAX_BYTES, b" ")
        sequence = self.directory / "sequence.json"
        sequence.write_bytes(source)
        cli = ["./agentpad", "play", sequence]
        self.assertEqual(pad.dry_run(source), source)
        recording = subprocess.check_output(cli, timeout=10)
        self.assertEqual(recording, source)
        sequence.write_bytes(recording)
        with fake_server(self.path, fast=True):
            subprocess.run([*cli, "--socket", self.path], timeout=10, check=True)
        self.assertFalse(self.path.exists())
        sequence.write_bytes(source + b"\n")
        rejected = subprocess.run(cli, timeout=5, capture_output=True, check=False)
        self.assertEqual(rejected.returncode, 1)
        self.assertIn(b"exceeds 1 MiB", rejected.stderr)
        self.assertEqual(rejected.stdout, b"")

    def test_queued_play_after_idle_frame_times_out(self):
        with fake_server(self.path), socket.socket(socket.AF_UNIX) as first:
            first.settimeout(5)
            first.connect(str(self.path))
            pad.send(self.path, SEQUENCE.ljust(pad.MAX_BYTES, b" "))
            reply = pad.receive_frame(first, pad.MAX_REPLY_BYTES, 1)
            self.assertIn("timed out", json.loads(reply)["error"])
            pad.send(self.path, SEQUENCE)

    def test_teardown_uses_original_directory_after_rename(self):
        private = self.directory / "session"
        private.mkdir(mode=0o700)
        moved = self.directory / "moved"
        with fake_server(private / "pad.sock"):
            private.rename(moved)
            private.mkdir(mode=0o700)
            replacement = private / "pad.sock"
            replacement.write_text("new owner")
            pad.send(moved / "pad.sock", SEQUENCE)
        self.assertFalse((moved / "pad.sock").exists())
        self.assertEqual(replacement.read_text(), "new owner")

    def test_replacement_socket_survives_teardown(self):
        with socket.socket(socket.AF_UNIX) as replacement:
            with fake_server(self.path):
                self.path.unlink()
                replacement.bind(str(self.path))
            self.assertTrue(self.path.is_socket())

    def test_existing_endpoint_survives_failed_bind(self):
        self.path.write_text("occupied")
        with patch.object(pad, "open_pad") as backend, self.assertRaises(OSError):
            pad.serve(self.path)
        backend.assert_not_called()
        self.assertEqual(self.path.read_text(), "occupied")

    def test_backend_startup_failure_and_cleanup(self):
        with patch.object(pad, "open_pad", side_effect=RuntimeError("device unavailable")):
            with self.assertRaisesRegex(RuntimeError, "device unavailable"):
                pad.serve(self.path)
            self.assertFalse(self.path.exists())
            with (patch.object(os, "unlink", side_effect=PermissionError("unlink failed")),
                  self.assertRaisesRegex(RuntimeError, "device unavailable") as error):
                pad.serve(self.path)
            self.assertIsInstance(error.exception.__cause__, PermissionError)

    def test_connect_uses_verified_endpoint_after_rename(self):
        original_inode = pad.socket_inode
        moved = self.directory / "moved.sock"

        @contextmanager
        def move_after_open(directory, name):
            with original_inode(directory, name) as fd:
                self.path.rename(moved)
                self.path.write_text("replacement")
                yield fd

        with fake_server(self.path), patch.object(pad, "socket_inode", move_after_open):
            pad.send(self.path, SEQUENCE)
        self.assertEqual(self.path.read_text(), "replacement")

    def test_client_checks_peer_before_writing(self):
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.path))
            listener.listen()
            with (patch.object(pad, "check_peer", side_effect=ValueError("wrong UID")),
                  self.assertRaisesRegex(ValueError, "wrong UID")):
                pad.send(self.path, SEQUENCE)
            listener.settimeout(1)
            peer, _ = listener.accept()
            with peer:
                peer.settimeout(1)
                self.assertEqual(peer.recv(1), b"")

    def test_private_parent_required_by_both_modes(self):
        self.directory.chmod(0o755)
        for operation in (lambda: pad.send(self.path, SEQUENCE), lambda: pad.serve(self.path)):
            with self.assertRaisesRegex(ValueError, "private"):
                operation()

    def test_response_validation_before_cli_success(self):
        # An ordinary test listener returns canned responses in place of a pad.
        for data in (b"null", b"[]", b"{}", b'{"ok":1}', b'{"error":[]}',
                     b'{"ok":true,"error":"unknown"}', b"truncated", b"\xff"):
            with self.subTest(data=data), socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(self.path))
                listener.listen()
                sequence = self.directory / "test.json"
                sequence.write_bytes(SEQUENCE)
                with subprocess.Popen(["./agentpad", "play", sequence, "--socket", self.path],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
                    listener.settimeout(5)
                    peer, _ = listener.accept()
                    with peer:
                        self.assertEqual(pad.receive_frame(peer, pad.MAX_BYTES, 1), SEQUENCE)
                        pad.write_frame(peer, data, 1)
                    stdout, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 1)
                self.assertFalse(stdout)
                self.assertIn(b"agentpad: invalid controller response", stderr)
                self.assertNotIn(b"Traceback", stderr)
                self.path.unlink()


class FrameTests(unittest.TestCase):
    def test_total_receive_deadline_covers_header_and_body(self):
        peer = Mock()
        peer.recv.side_effect = [pad.LENGTH.pack(2), b"a", b"b"]
        with (patch.object(pad.time, "monotonic", side_effect=[10, 10, 10.5, 11]),
              self.assertRaisesRegex(TimeoutError, "deadline")):
            pad.receive_frame(peer, pad.MAX_REPLY_BYTES, 1)
        self.assertEqual(peer.recv.call_count, 2)

    def test_length_checked_before_body_read(self):
        for limit in (pad.MAX_BYTES, pad.MAX_REPLY_BYTES):
            for size in (0, limit + 1):
                peer = Mock()
                peer.recv.return_value = pad.LENGTH.pack(size)
                with self.subTest(size=size), self.assertRaisesRegex(ValueError, "length"):
                    pad.receive_frame(peer, limit, 1)
                peer.recv.assert_called_once_with(pad.LENGTH.size)

    def test_reply_follows_all_snapshots_and_neutral_release(self):
        server, client = socket.socketpair()
        with server, client:
            pad.write_frame(client, SEQUENCE, 1)
            emitted = []
            pad.handle_peer(server, emitted.append)
            self.assertEqual(emitted, [*pad.parse(SEQUENCE), pad.frame(0)])
            reply = json.loads(pad.receive_frame(client, pad.MAX_REPLY_BYTES, 1))
            self.assertEqual(reply, {"ok": True})

    def test_half_close_cancels_and_releases(self):
        server, client = socket.socketpair()
        with server, client:
            pad.write_frame(client, SEQUENCE, 1)
            client.shutdown(socket.SHUT_WR)
            emitted = []
            pad.handle_peer(server, emitted.append)
            reply = json.loads(pad.receive_frame(client, pad.MAX_REPLY_BYTES, 1))
            self.assertIn("client disconnected", reply["error"])
            self.assertEqual(emitted, [pad.frame(0)])

    def test_error_reply_fits_limit_even_with_unicode(self):
        server, client = socket.socketpair()
        with (server, client, patch.object(pad, "parse", side_effect=ValueError("🎮" * 1000))):
            pad.write_frame(client, SEQUENCE, 1)
            pad.handle_peer(server, Mock())
            reply = json.loads(pad.receive_frame(client, pad.MAX_REPLY_BYTES, 1))
            self.assertTrue(reply["error"].startswith("🎮"))

    def test_linux_peer_credentials_checked_before_receive(self):
        server, client = socket.socketpair()
        with server, client:
            pad.check_peer(server)
            with patch.object(os, "getuid", return_value=os.getuid() + 1):
                emit = Mock()
                pad.handle_peer(server, emit)
                emit.assert_not_called()
                with self.assertRaisesRegex(ValueError, "same UID"):
                    pad.check_peer(client)
            reply = json.loads(pad.receive_frame(client, pad.MAX_REPLY_BYTES, 1))
            self.assertIn("same UID", reply["error"])
