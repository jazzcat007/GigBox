# Contributing

Thanks for looking. This tool writes to the flash of other people's synthesizers, so the bar
is "can you show it?", not "does it look right?".

## Running the suites

No hardware and no `m-flasher` are needed.

```bash
cd fm1-switch
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest -q          # Python: web API, guided flash on a fake bus, parser…
node --test tests/js/fleet.test.mjs    # panel logic, with a hand-written fake DOM (Node ≥ 18)
```

Two parser tests need real firmware packages and skip without them. See
`fm1-switch/realtest/README.md`.

## What you can and cannot test without units

| Without hardware | Needs real FM-1s and the Pi |
|---|---|
| API refusals, confirmation checks, path containment | that `m-flasher --device` touches only that device |
| `guided.flash` on a fake bus (shuffled ports, broadcasts, failures) | soft-key behaviour and timing per firmware |
| `m-flasher` output parsing (from captured real output) | real write, backup and restore output of each m-flasher version |
| `.fwsc` parsing and the install verdict | whether a firmware really installs completely |
| panel logic (fake DOM) | layout and touch on the 800×480 resistive panel under cog |

If a change depends on the right-hand column, say so in the PR, and say whether you ran it
on hardware and what you saw.

## House style

* **Evidence or it didn't happen.** Cite `file:line`, a command and its output, or a
  hardware observation with its date. Label what you measured, what you derived from code,
  and what you assumed.
* **Say when something is a heuristic.** Don't turn a guess into a green tick in the UI
  or in a docstring.
* **Fixtures come from reality.** When you add a test for a tool's output, paste the output
  you captured, and say where and when. A fixture written to match the parser once hid a bug
  for weeks (see `tests/test_fm1_switch.py`, `REAL_INFO_V13`).
* **Docstrings say what the code does now.** If you change behaviour, change the docstring in
  the same commit.
* **Stdlib only on the Pi, and no build step.** Dev-only test tooling is fine if the Pi never
  needs it.

## The safety invariants

These are listed in the README and, with their enforcement sites, in
`docs/AUDIT-fleet-manager.md` §4. A change that touches any of them needs an argument in the
PR: what it changes, the trade-off, and the test that pins the new behaviour. "It was
simpler" is not an argument. The short list:

never guess a unit · two deliberate actions, the second naming the unit · only write in
download mode · match by flash id or write nothing · nothing silently filtered · back up first
or write nothing · one write at a time, refused not queued · the server re-verifies what was
confirmed · `m-flasher` gets `input="yes\n"` and its "Nothing written" is a failure · log every
request and outcome · never print to a stdout that is the panel.

## Commits

One logical change per commit, with a message that says why. Keep refactors separate from
behaviour changes.
