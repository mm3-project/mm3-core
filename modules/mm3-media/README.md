# mm3-media

Video, images, slideshows, audio and YouTube for MM3. The backend asks the
engine's media service (`ctx.media`) for the URL of a source or file and gives it
to its element, which plays it only through the engine's `mm3-player` and
`mm3-youtube` elements.

```yaml
# the sources are the user's, in the top-level media key
media:
  sources:
    photos: {kind: files, path: files/photos}

# the instance
- module: media
  id: photos
  position: fullscreen_below        # any preset works: a background or an overlay
  config:
    kind: slideshow                 # video | image | slideshow | audio | youtube
    source: photos
    interval: 20
    order: random
  grants:
    media: [photos]
```

![A slideshow in a cell](docs/screenshot.png)

## Configuration

| Option | Values | Default |
| --- | --- | --- |
| `kind` | `video`, `image`, `slideshow`, `audio`, `youtube` (required) | none |
| `source` | a source of the `media` key; required except for `youtube` | none |
| `file` | a file of a `files` source, for `video`, `image` and `audio` | none |
| `video_id` | the 11 characters of a YouTube video (required for `youtube`) | none |
| `fit` | `contain`, `cover` | `contain` |
| `loop` | play again when it ends | `true` |
| `muted` | never play sound (sound is only on the kiosk anyway) | `false` |
| `opacity` | 0 to 1 | `1` |
| `interval` | slideshow: seconds between images, at least 3 | `15` |
| `order` | slideshow: `name`, `date` (oldest first) or `random` | `random` |
| `transition` | slideshow: `fade` (1 second) or `none` | `fade` |
| `recursive` | slideshow: include subfolders | `true` |
| `on_touch` | `none`, or `takeover` (not with `youtube`) | `none` |

`random` shows every image once before one comes again and never shows the same
image twice running. The folder is read again every 10 minutes and when the
configuration changes; video files in it are skipped. A source whose `path` is one
file needs no `file`.

An instance can be any preset: on `fullscreen_below` it is a background, on
`fullscreen_above` an overlay.

## Touch

With `on_touch: takeover` a touch on the cell starts a full-screen takeover of
the same source, or of the image the slideshow shows at that moment. It needs
`engine.input.touch`. A swipe that starts on this element goes to it and does not
change the page, as for every module that declares `input.touch`.

## Errors

A source that does not exist, a missing grant, a source that is down, a stream that
ends and a browser that cannot play it show a short message in the cell, in the
language of the mirror, and never stop the instance. The player tries again every
30 seconds while the instance is shown. While the instance is hidden or the mirror
is suspended the element stops its players, so the engine can stop the child of a
stream 30 seconds later.

A `camera` source follows the camera rules of the media service (no frame is stored,
and images are off unless the configuration turns them on).

## Grants

The module declares `media.play`, `media.takeover`, `media.youtube` and
`input.touch`; each is used only when its grant is there:

| Grant | When it is needed |
| --- | --- |
| `media: [<source names>]` (or `["*"]`) | always except for `youtube`; the sources the instance may play |
| `media.takeover` with the same `media` grant | `on_touch: takeover` |
| `youtube: true` | `kind: youtube` (the shell then lets the page frame `youtube-nocookie.com`) |

Without a grant the instance keeps running and the cell says what to add.

## Notifications

Emits none. Consumes `CORE_CONFIG_CHANGED {version, sections}` (an engine event,
core spec 6.3): a slideshow reads its folder again, and an instance that showed an
error looks for its source again.
