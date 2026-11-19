#!/usr/bin/env python3
"""Archive a ReSkate sequence and observation, or print an archived dry-run."""

import argparse
import ctypes
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

# Support invoking the script directly, including from outside the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import controller

RESULTS = ("landing", "miss", "unknown")


def read_regular(path: Path) -> bytes:
    def open_input(name, flags):
        return os.open(name, flags | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY)

    with open(path, "rb", opener=open_input) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path}: expected a regular file")
        if info.st_size <= controller.MAX_BYTES:
            data = stream.read(controller.MAX_BYTES + 1)
            if len(data) <= controller.MAX_BYTES:
                return data
    raise ValueError(f"{path}: file exceeds {controller.MAX_BYTES} bytes")


def validate_sequence(data: bytes) -> None:
    last = controller.parse(data)[-1]
    if last["buttons"] or any(last["axes"].values()):
        raise ValueError("the sequence must end with a neutral frame")


def publish(source: Path, destination: Path) -> None:
    # Linux renameat2 checks for an existing destination in the rename itself.
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise OSError("atomic trial publication requires Linux renameat2") from exc
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = rename(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        rename_noreplace,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def record(sequence: Path, trial: Path, result: str, note: str) -> None:
    data = read_regular(sequence)
    validate_sequence(data)
    metadata = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "result": result,
        "note": note,
    }
    observation = (json.dumps(metadata, indent=2) + "\n").encode("utf-8")
    if len(observation) > controller.MAX_BYTES:
        raise ValueError("observation exceeds the archive size limit")

    with tempfile.TemporaryDirectory(dir=trial.parent, prefix=".reskate-") as staging:
        archive = Path(staging) / "trial"
        archive.mkdir(mode=0o700)
        (archive / "inputs.json").write_bytes(data)
        (archive / "observation.json").write_bytes(observation)
        publish(archive, trial)


def replay(trial: Path) -> bytes:
    observation = read_regular(trial / "observation.json")
    metadata = json.loads(
        observation.decode("utf-8"), object_pairs_hook=controller.unique_object
    )
    if (
        not isinstance(metadata, dict)
        or metadata.keys() != {"sha256", "result", "note"}
        or metadata["result"] not in RESULTS
        or not isinstance(metadata["note"], str)
    ):
        raise ValueError("invalid trial observation")
    data = read_regular(trial / "inputs.json")
    if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
        raise ValueError("archived input hash mismatch")
    validate_sequence(data)
    return controller.dry_run(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    writer = commands.add_parser("record", help="create a new trial directory")
    writer.add_argument("sequence", type=Path)
    writer.add_argument("trial", type=Path)
    writer.add_argument("result", choices=RESULTS)
    writer.add_argument("--note", default="")
    reader = commands.add_parser("replay", help="dry-run an archived sequence")
    reader.add_argument("trial", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "record":
            record(args.sequence, args.trial, args.result, args.note)
        else:
            sys.stdout.buffer.write(replay(args.trial))
    except (OSError, ValueError, RecursionError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
