# AgentPad

Give an agent an Xbox-style controller on Linux. The agent chooses the buttons, stick positions, and timing for an entire move in one CLI call. A stick flick can take 120 milliseconds even if the agent takes several seconds to decide what to try next.

Use **AgentPad** to experiment with raw controls, compare two moves, or give a model a turn at the pad. **AgentPad Reflex** is the working name for a planned Jev-assisted companion. The idea is for Jev to keep playing moment to moment while the main agent plans a longer move, then let the main agent call the move or take control. Reflex is a design proposal. This release provides direct control.

## Try a move

From the checkout, run a rehearsal with Python 3.10 or newer. A dry run uses only the standard library and opens no input device.

```sh
./agentpad play experiments/reskate/ollie-120.json
```

This candidate centers the pad, waits 300 ms, holds the right stick down for 120 ms, flicks up for 60 ms, and releases. The output is a JSON sequence you can save and replay. The [ReSkate experiment](experiments/reskate/README.md) includes calibration and a second candidate with a longer hold. The trick timings await in-game testing.

## Write a sequence

```json
{"frames":[
  {"at_ms":0,"axes":{"ry":1.0},"buttons":["a"]},
  {"at_ms":120,"axes":{"ry":-1.0}},
  {"at_ms":180}
]}
```

Each frame specifies the full state. Omitted controls return to neutral, so the middle frame also releases A. End a hold with a later frame. The player releases all controls again when the sequence ends.

| Field | Values |
| --- | --- |
| Time | Integer milliseconds from the start, strictly increasing, from 0 through 600000 |
| Sticks | `lx`, `ly`, `rx`, `ry` from −1 to 1. Right and down are positive. |
| Triggers | `lt`, `rt` from 0 to 1 |
| Buttons | `a b x y lb rb back start guide ls rs`. The last two click the sticks. |
| D-pad | `up down left right` in the buttons list. Opposite directions cancel on their axis. |

Save the document as UTF-8 JSON up to 1 MiB. Dry-run output normally expands omitted controls. If expanding the document would exceed the same limit, AgentPad returns the original validated bytes so the output can still be replayed.

## Connect the virtual pad

Live playback needs Linux, Python evdev, and write access to `/dev/uinput` for the account running AgentPad. The tested evdev version is in [requirements-live.txt](requirements-live.txt). If your distribution already supplies it, use that interpreter. To install the tested dependency in a project-local environment with Python 3.11 or newer:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-live.txt
```

Use an ordinary user account that already has the required device access. Live button and stick events go to local applications listening for gamepads, which may include Steam or a desktop controller mapper.

For one play, `./agentpad play FILE --real` creates a pad, plays the sequence, and disconnects. For agent turns or games that discover controllers on startup, keep one pad attached. Run this in the first terminal:

```sh
pad_dir=$(mktemp -d)
./agentpad serve --real --socket "$pad_dir/pad.sock"
```

In a second terminal, replace the example socket path below with the one printed after `ready`. This play keeps the pad neutral.

```sh
./agentpad play experiments/reskate/neutral.json --socket /path/from/ready/pad.sock
```

Use matching client and server versions in a private socket directory owned by your account. The server plays one sequence at a time. Keep the client connected for the duration of its move. Disconnecting cancels that move and releases the controls. Ctrl-C in the server terminal removes the virtual device. After a forced kill, start with a fresh private directory.

## Run the tests

```sh
python3 -B -m unittest discover -v
python3 -B experiments/reskate/test_trial.py
```

For the optional Linux device check, run `python3 -B test_live_controller.py` with evdev, udevadm, and SDL2 available. This test creates a virtual controller and sends only neutral inputs. The development host identified the pad as an Xbox 360 controller in SDL2. Steam Input mapping and in-game timing await verification. The CLI currently supports controller input only.

[MIT License](LICENSE)
