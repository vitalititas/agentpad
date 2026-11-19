"""Opt-in neutral-only hardware smoke test. Run with python3 -B test_live_controller.py."""
import ctypes
import os
import selectors
import signal
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path

from controller import NAME


def check_sdl(event):
    # Use the joystick subsystem without opening a window or using HIDAPI.
    os.environ["SDL_JOYSTICK_HIDAPI"] = "0"
    sdl = ctypes.CDLL("libSDL2-2.0.so.0")
    sdl.SDL_GetError.restype = ctypes.c_char_p
    sdl.SDL_JoystickNameForIndex.restype = ctypes.c_char_p
    sdl.SDL_JoystickPathForIndex.restype = ctypes.c_char_p
    if sdl.SDL_Init(0x2000):  # SDL_INIT_GAMECONTROLLER
        raise RuntimeError(sdl.SDL_GetError().decode())
    try:
        for index in range(sdl.SDL_NumJoysticks()):
            if sdl.SDL_JoystickPathForIndex(index) == os.fsencode(event):
                assert sdl.SDL_IsGameController(index), "SDL2 controller mapping unavailable"
                print(f"SDL2 recognized {sdl.SDL_JoystickNameForIndex(index)!r} as a GameController")
                return
        raise AssertionError("SDL2 did not enumerate the pad")
    finally:
        sdl.SDL_Quit()


def devices():
    return [path.parent for path in Path("/sys/class/input").glob("input*/name")
            if path.exists() and path.read_text().strip() == NAME]


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("timed out waiting for input device state")


def smoke():
    from evdev import InputDevice, ecodes

    assert not devices(), "an AgentPad already exists"
    with tempfile.TemporaryDirectory() as directory:
        socket_path = Path(directory) / "pad.sock"
        neutral = Path(directory) / "neutral.json"
        neutral.write_text('{"frames":[{"at_ms":0},{"at_ms":80}]}')
        subprocess.run(["./agentpad", "play", neutral, "--real"], check=True, timeout=5)
        wait_for(lambda: not devices())

        with subprocess.Popen(
            ["./agentpad", "serve", "--real", "--socket", socket_path],
            stdout=subprocess.PIPE, text=True,
        ) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    assert selector.select(5), "server readiness timed out"
                    assert process.stdout.readline().startswith("ready "), "server failed to start"
                path, = devices()
                events = wait_for(lambda: [Path("/dev/input") / entry.name
                                           for entry in path.glob("event*")
                                           if (Path("/dev/input") / entry.name).exists()])
                event, = events
                info = subprocess.check_output(["udevadm", "info", "--query=property", str(event)],
                                               text=True)
                assert "ID_INPUT_JOYSTICK=1" in info.splitlines()
                print(f"Kernel exposed {event}, udev classified it as a joystick")
                with closing(InputDevice(event)) as pad:
                    assert pad.name == NAME
                    axes = pad.capabilities()[ecodes.EV_ABS]
                    assert len(axes) == 8
                    assert len(pad.capabilities()[ecodes.EV_KEY]) == 11
                    for _ in range(2):
                        subprocess.run(["./agentpad", "play", neutral, "--socket", socket_path],
                                       check=True, timeout=5)
                        assert devices() == [path]
                        assert not pad.active_keys()
                        assert all(pad.absinfo(code).value == 0 for code, _ in axes)
                    print("Two plays reused one device with every axis and button neutral")
                check_sdl(event)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            assert process.returncode == 143
        assert not socket_path.exists()
        wait_for(lambda: not devices() and not event.exists())
        print("SIGTERM removed the socket and input device, server exited 143")


if __name__ == "__main__":
    smoke()
