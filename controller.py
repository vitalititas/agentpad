"""Validate, record, and play timed Xbox-style controller snapshots."""
import argparse
import json
import os
import select
import signal
import socket
import stat
import struct
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

AXES = {"lx": "ABS_X", "ly": "ABS_Y", "rx": "ABS_RX", "ry": "ABS_RY",
        "lt": "ABS_Z", "rt": "ABS_RZ"}
KEYS = {"a": "BTN_SOUTH", "b": "BTN_EAST", "x": "BTN_NORTH", "y": "BTN_WEST",
        "lb": "BTN_TL", "rb": "BTN_TR", "back": "BTN_SELECT", "start": "BTN_START",
        "guide": "BTN_MODE", "ls": "BTN_THUMBL", "rs": "BTN_THUMBR"}
DPAD = {"up", "down", "left", "right"}
NAME = "AgentPad Xbox 360 Controller"
MAX_BYTES = 1024 * 1024
MAX_MS = 600_000
IO_TIMEOUT = 2.0
MAX_REPLY_BYTES = 4096
LENGTH = struct.Struct("!I")
PEERCRED = struct.Struct("iII")  # Linux pid, uid and gid


@contextmanager
def cleanup(action, *args):
    """Run cleanup even on interruption, keeping a primary failure visible."""
    try:
        yield
    except BaseException as original:
        try:
            action(*args)
        except Exception as secondary:
            raise original from secondary
        raise
    else:
        action(*args)


def frame(at_ms, axes=None, buttons=()):
    return {"at_ms": at_ms, "axes": dict.fromkeys(AXES, 0.0) | (axes or {}),
            "buttons": sorted(buttons)}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def parse(data):
    if len(data) > MAX_BYTES:
        raise ValueError("sequence exceeds 1 MiB")
    document = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    if (type(document) is not dict or document.keys() != {"frames"}
            or type(document["frames"]) is not list or not document["frames"]):
        raise ValueError("expected an object with a nonempty frames array")
    previous = -1
    snapshots = []
    for number, item in enumerate(document["frames"], 1):
        prefix = f"frame {number}"
        if type(item) is not dict or item.keys() - {"at_ms", "axes", "buttons"}:
            raise ValueError(f"{prefix}: unexpected frame shape")
        at = item.get("at_ms")
        if type(at) is not int or not previous < at <= MAX_MS:
            raise ValueError(f"{prefix}: at_ms must increase and lie in [0, {MAX_MS}]")
        axes = item.get("axes", {})
        if type(axes) is not dict:
            raise ValueError(f"{prefix}: axes must be an object")
        for axis, value in axes.items():
            lower = 0 if axis in {"lt", "rt"} else -1
            if axis not in AXES or type(value) not in (int, float) or not lower <= value <= 1:
                raise ValueError(f"{prefix}: invalid axis or value for {axis!r}")
        buttons = item.get("buttons", [])
        if (type(buttons) is not list
                or any(type(b) is not str or (b not in KEYS and b not in DPAD) for b in buttons)
                or len(set(buttons)) != len(buttons)):
            raise ValueError(f"{prefix}: expected distinct supported lower-case buttons")
        snapshots.append(frame(at, axes, buttons))
        previous = at
    return snapshots


def dry_run(data: bytes) -> bytes:
    """Return a validated, replayable file, including its exact CLI line ending."""
    normalized = json.dumps({"frames": parse(data)}).encode() + b"\n"
    return normalized if len(normalized) <= MAX_BYTES else data


@contextmanager
def open_pad():
    try:
        from evdev import AbsInfo, UInput
        from evdev import ecodes as e
    except ImportError as exc:
        raise RuntimeError("real mode requires Python evdev") from exc
    axis_codes = {axis: getattr(e, name) for axis, name in AXES.items()}
    keys = {button: getattr(e, name) for button, name in KEYS.items()}
    abs_codes = [
        (code, AbsInfo(0, -32768, 32767, 0, 0, 0) if axis not in {"lt", "rt"}
         else AbsInfo(0, 0, 255, 0, 0, 0)) for axis, code in axis_codes.items()
    ]
    abs_codes.extend((code, AbsInfo(0, -1, 1, 0, 0, 0))
                     for code in (e.ABS_HAT0X, e.ABS_HAT0Y))
    device = UInput({e.EV_KEY: list(keys.values()), e.EV_ABS: abs_codes},
                    name=NAME, vendor=0x045e, product=0x028e, version=0x0114)
    with cleanup(device.close):
        def emit(snapshot):
            buttons = set(snapshot["buttons"])
            for axis, value in snapshot["axes"].items():
                scale = 255 if axis in {"lt", "rt"} else (32768 if value < 0 else 32767)
                device.write(e.EV_ABS, axis_codes[axis], round(value * scale))
            device.write(e.EV_ABS, e.ABS_HAT0X, int("right" in buttons) - int("left" in buttons))
            device.write(e.EV_ABS, e.ABS_HAT0Y, int("down" in buttons) - int("up" in buttons))
            for button, code in keys.items():
                device.write(e.EV_KEY, code, int(button in buttons))
            device.syn()

        with cleanup(emit, frame(0)):
            emit(frame(0))
            yield emit


class ClientCancelled(ValueError):
    pass


def playback(snapshots, emit, peer=None):
    start = time.monotonic()
    with cleanup(emit, frame(0)):
        for snapshot in snapshots:
            delay = max(0, start + snapshot["at_ms"] / 1000 - time.monotonic())
            if peer is None:
                time.sleep(delay)
            elif select.select([peer], [], [], delay)[0]:
                raise ClientCancelled("client disconnected or sent extra data")
            emit(snapshot)


@contextmanager
def socket_directory(path):
    """Resolve bind/connect and cleanup relative to one verified Linux directory."""
    if path.name in ("", ".."):
        raise ValueError("expected a socket filename")
    fd = os.open(path.parent, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY)
    with cleanup(os.close, fd):
        parent = os.fstat(fd)
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ValueError("socket parent must be owned by you and private (mode 0700)")
        yield fd


@contextmanager
def socket_inode(directory, name):
    fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory)
    with cleanup(os.close, fd):
        info = os.fstat(fd)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("expected a socket owned by you")
        yield fd


def check_peer(peer):
    _, uid, _ = PEERCRED.unpack(peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                PEERCRED.size))
    if uid != os.getuid():
        raise ValueError("socket peer must have the same UID")


def unlink_owned(directory, name, owned):
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(current.st_mode) and os.path.samestat(current, os.fstat(owned)):
        os.unlink(name, dir_fd=directory)


def read_exact(peer, size, deadline):
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("frame receive deadline exceeded")
        peer.settimeout(remaining)
        chunk = peer.recv(min(65536, size - len(data)))
        if not chunk:
            raise ValueError("incomplete socket frame")
        data.extend(chunk)
    return bytes(data)


def receive_frame(peer, limit, timeout):
    deadline = time.monotonic() + timeout
    size, = LENGTH.unpack(read_exact(peer, LENGTH.size, deadline))
    if not 0 < size <= limit:
        raise ValueError(f"invalid socket frame length (expected 1 to {limit} bytes)")
    return read_exact(peer, size, deadline)


def write_frame(peer, data, timeout):
    peer.settimeout(timeout)
    peer.sendall(LENGTH.pack(len(data)) + data)


def handle_peer(peer, emit):
    try:
        check_peer(peer)
        snapshots = parse(receive_frame(peer, MAX_BYTES, IO_TIMEOUT))
    except (ValueError, RecursionError, OSError) as exc:
        reply = {"error": str(exc)}
    else:
        try:
            playback(snapshots, emit, peer)
        except ClientCancelled as exc:
            if exc.__cause__ is not None:  # Neutralization also failed. Retire the pad.
                raise
            reply = {"error": str(exc)}
        else:
            reply = {"ok": True}
    if "error" in reply:
        # ensure_ascii escapes a non-BMP code point as up to 12 ASCII bytes.
        reply["error"] = reply["error"][:(MAX_REPLY_BYTES - 32) // 12]
    try:
        write_frame(peer, json.dumps(reply).encode(), IO_TIMEOUT)
    except OSError:
        pass  # Playback is already neutral and the next client can be served.


def serve(path):
    with socket_directory(path) as directory, ExitStack() as stack:
        listener = stack.enter_context(socket.socket(socket.AF_UNIX))
        listener.bind(f"/proc/self/fd/{directory}/{path.name}")
        # Hold a reference so the inode cannot be recycled before cleanup.
        owned = stack.enter_context(socket_inode(directory, path.name))
        stack.enter_context(cleanup(unlink_owned, directory, path.name, owned))
        emit = stack.enter_context(open_pad())
        listener.listen()
        print(f"ready {path}", flush=True)
        while True:
            peer, _ = listener.accept()
            with peer:
                handle_peer(peer, emit)


def send(path, data):
    duration = parse(data)[-1]["at_ms"] / 1000
    with (socket_directory(path) as directory, socket_inode(directory, path.name) as owned,
          socket.socket(socket.AF_UNIX) as peer):
        peer.settimeout(IO_TIMEOUT)
        peer.connect(f"/proc/self/fd/{owned}")
        check_peer(peer)
        # One frame in each direction. Keep the write side open until the reply:
        # EOF (including SHUT_WR) or additional request bytes cancel playback.
        # Allow one preceding stalled request to time out before our turn.
        write_frame(peer, data, 2 * IO_TIMEOUT)
        timeout = duration + 3 * IO_TIMEOUT
        try:
            reply = json.loads(receive_frame(peer, MAX_REPLY_BYTES, timeout).decode("utf-8"),
                               object_pairs_hook=unique_object)
        except (ValueError, RecursionError, OSError) as exc:
            raise ValueError(f"invalid controller response: {exc}") from exc
    if type(reply) is dict:
        if reply.keys() == {"ok"} and reply["ok"] is True:
            return
        if reply.keys() == {"error"} and isinstance(reply["error"], str) and reply["error"]:
            raise ValueError(reply["error"])
    raise ValueError("invalid controller response")


class Interrupted(Exception):
    pass


def stop(signum, _):
    # Repeated stop signals should not interrupt neutralization or teardown.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    raise Interrupted(signum)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    play = commands.add_parser("play", help="print a replayable JSON recording by default")
    play.add_argument("file", type=Path)
    modes = play.add_mutually_exclusive_group()
    modes.add_argument("--real", action="store_true", help="create a uinput pad for this play")
    modes.add_argument("--socket", type=Path, help="play through an existing real session")
    session = commands.add_parser("serve", help="keep one real uinput pad connected")
    session.add_argument("--real", action="store_true", required=True)
    session.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        if args.command == "serve":
            serve(args.socket)
        else:
            with args.file.open("rb") as source:
                data = source.read(MAX_BYTES + 1)
            if args.socket:
                send(args.socket, data)
            elif args.real:
                snapshots = parse(data)
                with open_pad() as emit:
                    time.sleep(0.25)  # Give input consumers time to discover a one-shot pad.
                    playback(snapshots, emit)
            else:
                sys.stdout.buffer.write(dry_run(data))
    except Interrupted as exc:
        return 128 + exc.args[0]
    except (ValueError, RecursionError, OSError, RuntimeError) as exc:
        print(f"agentpad: {exc}", file=sys.stderr)
        return 1
    return 0
