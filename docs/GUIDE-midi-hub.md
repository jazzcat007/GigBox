# The MIDI hub: routing, channel layouts, a controller, the clock

The Pi that manages the units' firmware can also be the centre of a small dawless rig. It is a USB
MIDI host: it routes MIDI between the units and a controller, remaps and filters channels, and sends
a master clock. The units are class-compliant USB MIDI devices, so none of this needs any
cooperation from their firmware, and **nothing here writes to a unit's flash**.

The router is optional. With it off, the units work exactly as before.

## Turn it on

The router is a small service next to the web UI.

```bash
cp fm1-switch/systemd/fm1-router.service ~/.config/systemd/user/
systemctl --user enable --now fm1-router
```

It idles until a patch is chosen, follows edits to the patch file while running, and **lets go of
every unit before the soft key or a flash**, so it never stands between you and a write. If the Pi
is off, the units still work directly.

Use a **powered USB hub**: under-voltage was seen with four units on the Pi's own ports.

## Patches and routes

A **patch** is a named set of one-way **routes** between two ports. Ports are named by the unit's
letter key (`fm1-a`, `fm1-b`, ...) or by a controller name (see *A controller*, below), never by USB
card number: card numbers change on replug.

A route can:

- let through only some **channels**, or block kinds of message (`note`, `cc`, `program`,
  `pressure`, `bend`, `sysex`, `clock`, `transport`);
- **remap** every channel message onto one channel, or **shift** the channels up or down;
- **transpose** notes, scale **velocity**, and limit the **note range**.

Make and run them on the ROUTES screen of the touch UI (also on a phone), or from a shell:

```bash
fm1-switch route add default fm1-a fm1-b                       # A plays B, as is
fm1-switch route add default oxi fm1-c --channels 10 --block sysex
fm1-switch route list
fm1-switch route run default          # the service does this for you
fm1-switch route panic                # all sound off on every running unit
```

The router follows the patch named `default` unless the panel chose another (RUN / STOP on the
patch screen). **PANIC** sends all-sound-off, reset-controllers and all-notes-off on every channel of
every running unit.

## A controller (Oxi One and similar)

A USB MIDI device that is not an FM-1 can be a route's source or destination. It is found by the name
it gives the system, and it appears as a route key. The Oxi One is built in, as `oxi`.

- Only the controller's **first MIDI port** is routed (on the Oxi One, the one that carries the
  tracks). Its other ports are left alone.
- Two devices with the same name are skipped, because they cannot be told apart by name.
- A controller is **left out of the master clock and of panic**: it only receives or sends what a
  route names.
- Add another device by name in `router_service.EXTERNAL_DEVICES`.

Verified on the bench (2026-11-27): an Oxi One on the Pi's USB hub drives four units through the
router, and the notes sound on each.

## Channel layouts: one controller, four units, no channel set by hand

You have 16 MIDI channels on the controller and several units. A **layout** hands each unit a slice of
them, and the router translates each slice onto the channels the unit actually listens on, so
**nothing on the units has to be changed**.

For each unit you tell the Pi two things (Settings, or the **Channel layout** section of the expert
page at `/index.html`):

- **channels used**: how many channels the unit uses. A four-track firmware uses 4; a one-voice
  firmware uses 1.
- **listens from**: the first channel the unit itself is set to. Normally 1.

Then pick a layout. Two are always available, built from those counts:

| Layout | A (4) | B (4) | C (1) | D (1) |
|---|---|---|---|---|
| `packed`: slices follow each other, no gaps | 1-4 | 5-8 | 9 | 10 |
| `blocks`: each unit owns a block of 4, uses the first channels of it | 1-4 | 5-8 | 9 | 13 |

`blocks` keeps a unit's channels fixed when another unit changes. When there are many units the block
narrows so everything fits in 16: sixteen one-channel units get one channel each in either layout.
A layout that cannot fit is refused with the reason.

You can also save **custom** layouts by name (a start channel and a listens-from channel per unit), copy
a built-in one to edit, and delete your own. **Apply** writes an ordinary patch called `auto-<layout>`
and makes it the active one, so the router needs nothing new. Editing a unit's settings while a layout
is running rebuilds it.

From a shell:

```bash
fm1-switch route layout units fm1-a=4 fm1-b=4 fm1-c=1 fm1-d=1   # channels used (and :listens-from)
fm1-switch route layout list
fm1-switch route layout show blocks
fm1-switch route layout apply blocks
```

Notes:

- A unit set to **omni** (some one-voice firmwares) plays whatever channel it is sent, so its
  listens-from setting does not matter.
- The controller's clock, start and stop pass through every route, once per unit.
- A unit that you have set by hand to a different channel: put that channel in listens-from and the
  layout follows it.

## The master clock

The router can also send MIDI start, clock and stop to the units from a tempo you set. It runs from
the CLOCK screen, `fm1-switch clock set|start|stop|targets`, or `POST /api/router/clock`.

A router always starts with the clock **stopped**. A controller is never sent the clock unless it is
named as a target. *Tested against a fake sequencer; not yet run against the units.*

## What is verified

| | |
|---|---|
| Verified on the bench | all four units attach; a live route from one unit to another; the soft key works while the router holds the units; an Oxi One routed to four units through the `blocks` layout |
| Tested with fakes only | the master clock; the layout API and screens |
| Not measured | round-trip latency to a unit (the router hop itself measured 0.3 ms median in software) |

The roadmap, including ideas not built, is in [`ROADMAP-dawless-hub.md`](ROADMAP-dawless-hub.md).
