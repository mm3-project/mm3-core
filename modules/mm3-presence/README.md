# mm3-presence

Presence and display power for MM3, as a backend-only module. It follows sensors
and touches on the mirror, dims the display when nobody is there, turns it off
and asks the engine to suspend, and keeps the display off in the hours you
choose. The engine itself only offers the generic hooks (`ctx.system`), so you
can replace this module with a camera, Bluetooth or phone based one.

```yaml
- module: presence
  id: presence
  config:
    mqtt:
      - {topic: "home/hall/pir", field: occupancy}      # JSON {"occupancy": true}
      - {topic: "home/mirror/motion", value: "ON"}      # plain text
    idle_after: 5m
    idle_brightness: 20
    off_after: 15m
    schedule:
      - {off: "23:00", on: "06:30"}
      - {off: "09:00", on: "17:00", days: [mon, tue, wed, thu, fri]}
    display: ddcutil
  grants:
    mqtt: {subscribe: ["home/hall/*", "home/mirror/*"]}
```

Only one instance can run; a second one fails its start with
`CFG_SINGLE_INSTANCE`.

## States

| State | Meaning |
| --- | --- |
| `on` | Presence is held, or the count has not reached `idle_after` |
| `idle` | `idle_after` passed: a driver with brightness is set to `idle_brightness` |
| `off` | `off_after` passed, a schedule window, or `POST display/off`: the display is turned off and the module calls `request_suspend("presence")` |

Presence is held while any topic or sensor has it set. From the moment it is no
longer held, or from a moment of presence (a touch, `POST display/on`), the
module counts. A moment of presence while `idle` or `off` makes the state `on`:
the module calls `request_resume("presence")`, turns the display on and sets the
brightness back to `brightness`, or to the level before `idle` when `brightness`
is not set. Every change is broadcast as `PRESENCE_STATE {state, reason}` with
reason `start`, `presence`, `timeout`, `schedule` or `command`.

## Configuration

| Option | Values | Default |
| --- | --- | --- |
| `mqtt` | list of `{topic, field?, value?}`; each topic needs a `mqtt.subscribe` grant | `[]` |
| `touch` | a touch on the kiosk counts as presence (`CORE_INTERACTION`) | `true` |
| `idle_after` | duration; `0` never goes idle; must be below `off_after` | `0` |
| `idle_brightness` | 0-100 | `30` |
| `brightness` | 0-100, or empty to leave the display as it is | empty |
| `off_after` | duration above 0 | `10m` |
| `schedule` | windows `{off: "HH:MM", on: "HH:MM", days?}` | `[]` |
| `wake_in_schedule` | presence can turn the display on inside a window | `false` |
| `display` | driver name | `kiosk` |
| `display_options` | options of the driver | `{}` |

A message on a topic sets presence when it matches and clears it otherwise. With
`field`, it matches when the value at that dotted path of the JSON payload equals
`value` (default `true`); without `field`, when the payload text equals `value`
(default `ON`), ignoring case. `PRESENCE_DETECTED` and `PRESENCE_CLEARED` from
other modules set and clear presence for their sender and `sensor`.

An `on` not after `off` ends the next day; `days` are the days a window starts.
Inside a window the state is `off` with reason `schedule` and presence is
ignored (unless `wake_in_schedule`). Overlapping windows act as one. Times are
read in the mirror's time zone; a time skipped by a clock change is the next
minute that exists.

## Display drivers

| Driver | What it does | Options |
| --- | --- | --- |
| `kiosk` | The kiosk agent switches the output with `wlr-randr`; no brightness | none |
| `ddcutil` | DDC/CI: VCP 0x10 brightness, 0xD6 power (1 on, 4 off) | `display` (default 1) |
| `cec` | HDMI-CEC with `cec-client -s -d 1`: `on 0`, `standby 0`; no brightness | `adapter` |

`ddcutil` and `cec-client` come from the Debian packages `ddcutil` and
`cec-utils`; the installer adds them and puts the mirror user in the groups
`i2c` and `dialout`. Other drivers are plug-ins on the entry point group
`mm3_presence.display`: a class built with `(ctx, options)` that has `name`,
`supports_brightness`, `async set_power(on)`, `async set_brightness(percent)` and
`async check()`, which returns `None` when it can work or a reason. A driver that
also has `async get_brightness()` lets the module restore the level before `idle`.

A failed driver call is logged and shown in the health field `driver_error`; the
state changes anyway and the call is tried again at the next change.
`mm3 diag check` reports the driver and its reason.

## Control

Under `/api/v1/modules/<id>/`:

- `GET state` returns `{state, reason, since, held, driver}`.
- `POST display/on` is a moment of presence (reason `command`).
- `POST display/off` turns the display off at once (reason `command`) until the
  next presence.

With the MQTT bridge, Home Assistant gets a switch `display`, a binary sensor
`presence`, a sensor `state` and, when the driver supports brightness, a number
`brightness` that replaces the configured one until the next start.

## Capabilities

`system.power`, `mqtt.subscribe`, `ha.discovery`, `api.routes`.

## Grants

`mqtt: {subscribe: [...]}` with the topics of `mqtt`, for example
`["home/hall/*"]`. A topic without a grant is skipped: the instance keeps
running (touch and the other sources still work), logs a warning and shows the
health fields `mqtt_error` and `mqtt_topics_without_grant`. The module needs no
HTTP grant. The `ddcutil` and `cec` drivers run local programs and need no
grant; they need the `i2c` or `dialout` group (see above).

## Notifications

Emits `PRESENCE_STATE {state, reason}` at start and on every change (registered,
core spec 5.5). Consumes `PRESENCE_DETECTED {sensor, zone?}`,
`PRESENCE_CLEARED {sensor, zone?}` and `CORE_INTERACTION {kind}`.
