# mm3-greeting

Shows a greeting for the time of day.

```yaml
- module: greeting
  id: hello
```

![Screenshot](docs/screenshot.png)

## Notifications

Emits none, consumes none.

## Media demo (milestone 8 test)

To try `mm3-player` and `mm3-youtube` on the mirror before `ctx.media` exists,
an instance can play one URL of the media service or one YouTube video under
the greeting. This is a test option of this unpublished module only: real
modules get media URLs from `ctx.media` (Module Contract MOD-100, MOD-102).

```yaml
- module: greeting
  id: hello
  config:
    media_demo: { src: /media/door/stream.mjpg, fit: cover }
- module: greeting
  id: clip
  grants: { youtube: true }
  config:
    media_demo: { youtube: dQw4w9WgXcQ, muted: true }
```

Sound plays only on the kiosk, unless `media.browser_audio` is true (core CORE-409).

## Capabilities

`media.play` (`mm3-player`) and `media.youtube` (`mm3-youtube`), used only by `media_demo`.

## Grants

`youtube: true` for `media_demo.youtube`; nothing else.
