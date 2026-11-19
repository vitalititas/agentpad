# ReSkate S.K.A.T.E. trial archive

Record a proposed controller sequence and a human observation with the game
closed. Replay prints the normalized frame schedule immediately. Both commands
run offline and send no controller input.

## Record and replay

From the AgentPad directory containing `agentpad` and `controller.py`, run

```sh
trials=$(mktemp -d)
./agentpad play experiments/reskate/stick-calibration.json
./experiments/reskate/trial.py record experiments/reskate/ollie-120.json "$trials/first" unknown --note "Dry-run rehearsal"
./experiments/reskate/trial.py replay "$trials/first"
```

Choose a new trial name under an existing directory. On Linux the recorder
writes both files in a temporary directory on the same filesystem, then
publishes them together using `renameat2(RENAME_NOREPLACE)`. An occupied name,
including an empty directory, makes the command fail. A filesystem or libc
without that operation also returns an error.

- `inputs.json` contains the exact submitted bytes. Each frame replaces the
  previous snapshot. Omitted controls are neutral. The recorder and replay
  validate every frame against AgentPad's sequence format and require a
  neutral final frame.
- `observation.json` contains the lowercase SHA-256 of those input bytes, the
  result (`landing`, `miss`, or `unknown`), and the note. Each file must be a
  regular file of at most 1 MiB. The reader rejects symbolic links to either
  file.
- Replay checks the hash and passes the same input bytes to AgentPad's dry-run
  formatter. Its stdout matches `./agentpad play FILE`. Use the archived input file to check the digest because dry-run output may expand omitted controls.

The hash covers `inputs.json`, including whitespace and line endings. Editing
only the note or result leaves the hash unchanged. Someone who can replace the
input and its stored digest can make another sequence pass the check. The
archive records the submitted schedule and what the observer wrote. It cannot
establish authorship, actual device timing, manual input, or an in-game result.

## Notes for a future game session

The right-stick mapping and ollie timings are hypotheses awaiting in-game
confirmation. Each example ends with a neutral frame.

| Input | What the observer should check |
| --- | --- |
| `neutral.json` | Whether the skater moves during the 500 ms neutral hold. |
| `stick-calibration.json` | Half-left, half-right, full-down and full-up, each followed by a return to center. Right-stick down uses positive values. |
| `ollie-120.json` | Hold down for 120 ms, switch to up for 60 ms, then release. |
| `ollie-240.json` | Same stick positions and up hold, with a 240 ms down hold. |

For a separately agreed session with the player, check the in-game bindings
and calibration first. Reset to the same flat spot, stance and starting speed
between attempts. Record those conditions, the displayed trick name, any manual
input, and what the observer saw. Use `landing` for a visibly landed intended
trick, `miss` for a visible failure or different trick, and `unknown` for an
unclear result or an offline rehearsal.

Run `python3 -B experiments/reskate/test_trial.py` from the AgentPad directory
for the offline archive checks.
