# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Module API changes are listed
separately under **Module API** in each release (Module Contract, section 10).

## [Unreleased]

### Changed (milestone 8 step 9, spec sync: Core spec 0.58)

- Core spec 0.58 changes CORE-412 (`GET /api/v1/media` also gives each source `kind`, `delivery`,
  `device` and `path`, never `url`) and CORE-414 (the Media page `/admin/media`: Sources, Files and Shares,
  how it reads its data and what each action does). Spec sync in this PR: `core.md`,
  `requirements.toml`, `versions.toml`, `coverage.md`, `milestones.toml` (comment), `docs/rules.md`
  1.87 and the versions line of `docs/log/README.md`. The same edits are to be made in the master
  document of the Core spec. The code follows in the next PR.

### Added (milestone 8 step 8 follow-up: the MOD-102 row and its static check; Contract 1.0 rev 17)

- Module Contract 1.0 rev 17 adds the row of MOD-102 to section 11 (MUST, static) and a
  Document history row; no MOD id changed (spec sync in this PR: `contract.md`,
  `requirements.toml`, `versions.toml`). The other media rules of section 4.5 get no row yet.
- `mm3 module check` has a static MOD-102 row (`mm3.core.modcheck.jsscan.scan_media`, run from
  `static.py`): in a front-end that uses `mm3-player` or `mm3-youtube`, or whose module declares
  a `media.*` capability, it fails `<video>`, `<audio>`, `<iframe>`, `<source>`, `<embed>`,
  `<object>`, an `<img>` whose `src` comes from data, `new Audio()`, `new Image()`, assignments to
  `src`, `srcObject` and `srcset` and `setAttribute('src' | 'srcset' | 'data')`; and for a
  front-end that uses a player it fails a script that never handles `mm3-media-error`, or whose
  `onSuspend` is missing or stops nothing. `mm3-media` passes (MOD-102).

### Added (milestone 8 step 8: the reference module `mm3-media`; MED-01..08, MOD-102)

- `modules/mm3-media`, the fourth reference module (MED-01): one distribution, published with the
  other default modules and listed in `mm3-modules-default` (CORE-294). Kinds `video`, `image`,
  `slideshow`, `audio` and `youtube` through `ctx.media`; capabilities `media.play`,
  `media.takeover`, `media.youtube` and `input.touch`, grant `media: [<source>]` (MED-01, MED-02).
  The configuration is validated at load with messages from the catalog (a `youtube` kind needs
  `video_id` and takes no `source` or takeover, the other kinds need a `source`).
- Slideshow (MED-03): the files of a folder (optionally with subfolders) in `random` rounds
  without repeats or in `name` or `date` order (oldest first), shown every `interval` seconds
  (at least 3) with a fade; the list is read again every 10 minutes and on
  `CORE_CONFIG_CHANGED`, a folder that does not answer in time keeps the old list.
- Messages in the cell for a missing grant, a missing file, an empty folder and every
  `mm3-media-error` reason of the player, which stays in place and tries again (MED-04); a touch
  on the cell starts a takeover when `on_touch` is `takeover` (MED-05); the players stop in
  `onSuspend` and the slideshow timer pauses (MED-06); texts in EN, IT, FR, ES and DE (MED-07);
  tests with the fake media service and in a browser, `mm3 module check` and a README with the
  grants table (MED-08). The front-end uses only `mm3-player` and `mm3-youtube` with URLs from
  the state, and sets no media URL of its own (MOD-102).
- `mm3 module check` counts `ctx.media.sources`, `url` and `files` as uses of `media.play` and
  `takeover` and `stop` as uses of `media.takeover` (MOD-70); the check still warns that
  `mm3-media` declares `input.touch` without using it (MOD-71), because it cannot see touch
  input in the front-end. The row of MOD-102 in Contract section 11 and its static check follow
  in the next entry.
- CI and the release workflow build and publish `mm3-media` with the other default modules; the
  PyPI project needs its trusted publisher set up once, like the others.

### Added (milestone 8 step 7: `ctx.media`, the fake media service; MOD-100, MOD-101, MOD-103)

- `ctx.media` (`mm3.media`, `mm3.core.media.instance`): `sources()` lists the granted sources
  with kind, delivery, audio and state; `url(source, file=)` gives a path on the engine's origin
  (`/media/<source>/stream.mjpg`, `/media/<source>/hls/index.m3u8`, `/media/files/<path>`);
  `files(source, path)` lists a folder of a `files` source on the media folder threads (a folder
  that does not answer in 5 s is `TransientError`); `takeover(source, file=, duration=)` returns
  the id of a takeover that starts as `MEDIA_TAKEOVER` does (queued in order, a refusal logged and
  dropped); `stop(id=)` is `MEDIA_STOP`, without an id only for the takeover the instance started
  last. `sources()`, `url()` and `files()` need `media.play`, `takeover()` and `stop()` need
  `media.takeover`, every call needs a `media` grant for the source, and the context raises
  `PermissionError` for a missing capability (reported like any denied capability, MOD-70) or
  grant. An unknown source, a `file` on a source that is not a `files` source, a missing or unsafe
  `file`, a `duration` of 0 or less and a folder that is not one are `ValueError` (MOD-100,
  MOD-101).
  **Part not built:** `url()` looks a file up only in `files/`; a file under a share is not
  checked, because a share may be a network mount and `url()` is synchronous, so the player
  reports it as unavailable (MOD-100 names an unknown file as a `ValueError`).
- The takeover service queues `MEDIA_TAKEOVER` and `MEDIA_STOP` (from the bus and from
  `ctx.media`) so a start and a stop sent one after the other keep their order (CORE-411).
- Test kit: `ModuleHarness.media` is `FakeMedia` (`add_source`, `add_files`, `set_state`, `end`;
  recorded `takeovers` and `stops`, `current`, `list_error`), and `BrowserHarness(media=...)`
  serves fixture files under `/media/...` and `/admin/preview/media/...`, so a state built from
  `ctx.media` URLs plays in the browser harness (MOD-103).
- `mm3.core.media` loads its service on first use, so `ctx.media` and the folder helpers import
  without the configuration package (no import cycle with the supervisor).

### Fixed (ISS-020; CORE-361)

- A restore from an uploaded archive deletes the staged file before it shows its final state
  (`done`, `rolled_back` or `failed`), not just after it, so a client that checks at once no
  longer finds it; a new test checks the file at the moment the final state is written.
### Changed (milestone 8 step 6 follow-ups; spec 0.57: CORE-405, CORE-406, CORE-407 changed)

- The file delivery route `/media/files/…` (opening and every read) and the takeover's file
  checks and length probe run on the media folder threads with the 5 s timeout, like the admin
  file routes, so a hung mount never takes the engine's default threads (CORE-407, CORE-400).
- The Backups page offers the `media` part, off by default, with its label in the five
  languages (CORE-408).
- Core spec 0.57 records the user's decisions after the step 6 review: a failed upload write
  answers 503, a removed share's copy goes at the installer's next pass, and the SMB password is
  revealed only when it changes, an accepted risk (spec sync PR #156); citations of CORE-405..407
  moved to 0.57.

### Added (milestone 8 step 6a: files in the admin and the media backup part; CORE-404, CORE-405, CORE-408)

- File routes of the admin on the admin listener and the admin socket (`mm3.core.media.files`,
  `mm3.core.web.mediafiles`): `GET /api/v1/media/files?path=` lists a folder of `files/` or
  `shares/` (`shares` alone lists the shares) as `{name, kind, size, modified}` with `kind`
  `folder`, `video`, `audio` or `image`, skipping links and other extensions;
  `POST /api/v1/media/files?path=&name=[&overwrite=true]` takes the raw body, refused from its
  `Content-Length` above `media.limits.upload_max_mb` (413 `CORE_API_UPLOAD_TOO_LARGE`), writes
  a hidden temporary file and links (or renames) it to its name, removes it when the body does
  not match its length or a write fails, and answers 201; `POST /api/v1/media/folders` `{path}`
  and `DELETE /api/v1/media/files?path=[&recursive=true]`; writes under `shares/` are 403, and a
  folder that does not answer in 5 s is 503. Folder calls run on a small executor of their own,
  so a hung mount never takes the engine's default threads (CORE-405, CORE-404).
- The `media` backup part: `backup.parts`, `POST /api/v1/backups` and `mm3 backup --parts`
  accept `media` (never a default); it holds the media folder's `files/` under `media/`; a
  restore that selects it replaces `files/` whole and never touches `shares/`, and only then
  does the pre-restore backup include `media` (CORE-408).

### Changed (spec 0.55; CORE-405, CORE-406, CORE-407, CORE-408 changed)

- Core spec 0.55 settles the details of the file routes, the `media` part, how the installer
  applies shares and the mount options (spec sync PR #152).
### Added (milestone 8 step 6b: network shares; CORE-406, CORE-407)

- Shares by sync (`mm3.installer.shares`): the installer reads `media.shares` over the admin
  socket once a minute, keeps the share states in `/var/lib/mm3-install/shares.json` (`idle`,
  `syncing`, `ok`, `error` with `remote`, `too_large`, `failed`, `mount` or `path`) and runs due
  syncs as `media_sync` operations that wait while another operation runs; a run checks the
  remote's type, measures it with `rclone size` against `max_size_gb`, then runs `rclone sync`
  without capabilities (`setpriv`, umask 022) for the extensions of CORE-404 only, without
  links, into `shares/<name>/` opened without following a link and owned by `mm3-install`
  (mode 0755), passed as `/proc/self/fd/<n>`; copies of removed shares are deleted;
  `GET /v1/media/shares` and `POST /v1/media/shares/<name>/sync` on the installer socket
  (CORE-406).
- Shares by mount: the installer writes only `mounts/shares.json` and the SMB credentials files
  (0600, from the rclone remote, the password revealed only when it changes) in its own state;
  the root systemd generator `mm3-media-mounts` (shipped in the package, printed by
  `mm3 installer generator`, installed by `mm3-install.sh` into
  `/usr/lib/systemd/system-generators/`) checks every entry again and makes
  `var-lib-mm3-media-shares-<name>.mount` and `.automount` (`ro,nosuid,nodev,noexec,_netdev`,
  NFS `soft,timeo=100,retrans=3`, `TimeoutSec=10s`, `TimeoutIdleSec=600`, wanted by
  `remote-fs.target`); the installer reloads systemd, starts and stops those units, and marks a
  failed mount `error` with reason `mount`; the polkit rule allows the reload and start, stop
  and restart of exactly those unit names; `mm3-install.sh` installs `cifs-utils` and
  `nfs-common` (CORE-407).
- The engine forwards `GET /api/v1/media/shares` and `POST /api/v1/media/shares/<name>/sync`
  (202 `{id}`) to the installer, reads the share states every 30 s while `media.shares` is not
  empty, and gives `files` sources under a share in `error` the state `unavailable` with the
  reason `share` (CORE-406).

### Changed (spec 0.56; CORE-406, CORE-407 changed)

- Core spec 0.56: the mount units are made by a root generator and named after their mount
  point, as systemd requires (spec sync PR #154).

### Added (milestone 8 step 5: the takeover; CORE-410, CORE-411, CORE-412, CORE-418)

- The full-screen takeover (`mm3.core.media.takeover`): one at a time, newest wins, holding the
  page pause of holder `media`; it ends by duration (default `media.takeover.duration`, shortened
  to `max_duration`), when a child exits with code 0, after a file's length read with `ffprobe`
  within 5 s, on a stop, on a kiosk touch (`CORE_INTERACTION`) and on a hot reload that removes
  or changes its source (CORE-410).
- `MEDIA_TAKEOVER` and `MEDIA_STOP` on the bus (modules need `media.takeover` and a `media`
  grant for the source, otherwise dropped with a warning; a module's `MEDIA_STOP` without an id
  stops only its own takeover), from MQTT when imported, `POST /api/v1/media/takeover` (202
  `{id}`, 422 `CORE_API_INVALID_REQUEST`) and `DELETE /api/v1/media/takeover[?id=]` (204); the
  Home Assistant select `media_takeover` and sensor `media_playing`, published again on every
  `CORE_MEDIA_CHANGED`; `GrantSet.media` (CORE-411).
- `CORE_MEDIA_CHANGED` and `GET /api/v1/media` carry the running takeover, and the debug
  overview shows it (CORE-412, CORE-410).
- `welcome.takeover` and the `control` action `takeover` for kiosk and browser clients; the
  shell shows it in an `mm3-player` in a layer above every preset, with the kiosk's sound, and
  the preview never shows it (CORE-418).

### Changed (spec 0.54; CORE-410, CORE-411 changed, CORE-418 added)

- Core spec 0.54, settled while building milestone 8 step 5: the engine ends a takeover when its
  child exits with code 0 or after a file's length read with `ffprobe`; images and live sources
  end by duration; a hot reload that removes or changes the source ends it; the preview never
  shows it (CORE-410). `duration` is shortened to `max_duration`; a bad duration or file, or a
  folder `files` source without `file`, is 422 from REST and dropped otherwise; generated ids of
  12 hex characters; `MEDIA_STOP` without an id from a module stops only its own takeover;
  `DELETE /api/v1/media/takeover[?id=]` answers 204; the options, commands and state of the HA
  select `media_takeover` (CORE-411). `welcome.takeover` and the `control` action `takeover` for
  kiosk and browser clients (CORE-418). Rules 1.81.

### Added (milestone 8 step 4: the player in the shell; CORE-409, CORE-413, CORE-417, spec 0.53)

- `mm3-player` in `/core/mm3.js` (CORE-409): plays an MJPEG URL in an image, an HLS URL natively
  and a file of the media folder as video, audio or image, with `src`, `fit`, `loop` and
  `muted`; sound only where `welcome.audio` is true (the kiosk, or every client with
  `media.browser_audio`), `muted` always wins, and a browser that refuses sound without a
  gesture plays muted; `mm3-media-error` with the reason found by asking the URL once (403
  `forbidden`, 404/503 `unavailable`, no answer `network`, a 200 that does not play `decode`,
  HLS without native support `unsupported`), `mm3-media-playing`, a retry every 30 s, and
  `stop()` / `start()` for suspended elements (MOD-27); in the preview `/media/` URLs play from
  `/admin/preview/media/`.
- `mm3-youtube` (CORE-413): the youtube-nocookie.com iframe with `referrerpolicy` and `allow`,
  only for an instance whose module declares `media.youtube` and whose grants have
  `youtube: true`; otherwise a placeholder and `mm3-media-error` `forbidden`. The shell page
  (`/` and `/admin/preview`) adds `frame-src https://www.youtube-nocookie.com` to its CSP only
  while such an instance is enabled.
- `welcome.audio` and `youtube` per instance (CORE-417); the instance grants `media` and
  `youtube` with labels in five languages; a hot reload that changes either resyncs the shells,
  and the shell re-creates the elements concerned.
- `mm3 module check`: using `mm3-player` or `mm3-youtube` in a front-end counts as using
  `media.play` or `media.youtube` (MOD-70, MOD-71, MOD-103).
- mm3-greeting (never published): `media_demo` plays one `/media/` URL or one YouTube video under
  the greeting, to try the player on the mirror before `ctx.media` (MOD-100) exists.

### Changed (spec 0.53; CORE-409, CORE-413 changed, CORE-417 added)

- Core spec 0.53, settled while building milestone 8 step 4: `mm3-player` plays HLS only where
  the browser does natively (Chromium 142 and later) and hls.js leaves the approved stack,
  because its MediaSource playback needs `media-src blob:`; in the preview shell `/media/` URLs
  play from `/admin/preview/media/`; `mm3-media-error` `{reason, status?}` (bubbling, composed;
  `unavailable`, `forbidden`, `network`, `decode`, `unsupported`), `mm3-media-playing` and a retry
  every 30 s (CORE-409). `mm3-youtube` takes `video-id`, `loop` and `muted`, follows the sound
  rule and dispatches `forbidden` (or `unavailable` for a bad id) with its placeholder (CORE-413).
  `welcome.audio`, `youtube` per instance, the grants `media` and `youtube`, and a resync when
  they change (CORE-417). Rules 1.80.

### Changed (spec 0.52; CORE-402 changed)

- Core spec 0.52: a relay of the local camera feed is not a child; while the feed cannot be
  read a camera source is `idle` with the reason `feed`, never `error` (a new viewer gets 503
  with `Retry-After: 5`, the relay connects again every 1 to 5 s); after an owner change to
  `media`, Media opens the device no earlier than 5 s after the hot reload (CORE-402). Rules 1.79.

### Added (milestone 8 step 3: camera sources and the local feed; CORE-394..396, CORE-402, spec 0.52)

- Local camera feed `mm3.core.feed` (CORE-394, CORE-395), standard library only: `FeedServer`
  for the owner of a device (the socket `/run/mm3/feed/<device>.sock`, mode 0660, group `mm3`;
  the header line, then 4-byte big-endian lengths and JPEG frames; the newest frame wins for a
  slow reader, and the owner learns when readers come and go so it captures only while read),
  and `FeedReader`, which refuses a header or a frame that breaks the limits (640 x 480, 30 fps,
  `jpeg`). `mm3-vision` can use either side or speak the same bytes; the engine itself offers no
  feed.
- Camera sources (CORE-402): owned by `vision`, a source relays the feed as MJPEG with no
  `ffmpeg` and outside `media.limits.processes`, connecting on the first viewer and leaving 30 s
  after the last; while the feed cannot be read it is `idle` with the reason `feed` (new viewers
  503 with `Retry-After: 5`, open viewers stay and the relay connects again every 1 to 5 s);
  owned by `media`, an `ffmpeg` child reads the V4L2 device (`-f v4l2`); owned by `none`, it is
  `unavailable` with the reason `owner`. The admin shows the reasons `feed` and `owner`; the
  placeholder reason `camera` of step 2 is gone.
- Owner change (CORE-396): a hot reload that changes a camera's owner ends its source; a camera
  child gets 3 s before it is killed, so the device is free within 5 s, and `devices.json` is
  written again only after that; after a change to `media` the device is opened no earlier than
  5 s after the reload.

### Fixed (CORE-75)

- `tools/restack.py` reports a refused push and a comment that cannot be posted with the error
  output of `git push` or `gh` (workflow `::error::` and `::warning::` lines) instead of a bare
  traceback; a failed comment no longer stops the other pull requests, and the run still fails.

### Added (milestone 8 step 2: media service and delivery; CORE-397, CORE-400, CORE-401, CORE-403, CORE-404, CORE-412, spec 0.51)

- Media service `mm3.core.media` (CORE-397), the only code that starts `ffmpeg`: a child starts
  on the first viewer and stops 30 s after the last; at most `media.limits.processes` children,
  and a request beyond the limit is 503 with `Retry-After: 5`; children run with `nice` 10; a
  child that fails is restarted with backoff (1, 2, 4 … 60 s) and five failures in ten minutes
  give the state `error` until the next hot reload; `ffmpeg`'s standard error is logged at debug
  level with URL credentials masked. `media` applies on hot reload: changed and removed sources
  stop.
- Delivery routes (CORE-400) on the mirror listener, and under `/admin/preview/media/` behind an
  admin session: `/media/<source>/stream.mjpg` (multipart JPEG, newest frame wins),
  `/media/<source>/hls/index.m3u8` with 2 s fragmented-MP4 segments and a list of 6 in
  `/run/mm3/media/<source>/` (deleted when the child stops), and `/media/files/<path>` with
  range requests. `auto` picks MJPEG for cameras and for `rtsp` without audio, HLS otherwise. A
  `camera` source is 403 while its device has `images: false`.
- Remux and transcode (CORE-401): codecs are copied by default; a copied HLS source that is not
  H.264 with AAC, MP3 or Opus gets the state `error` with the reason `codec`; with
  `transcode: true` H.264 comes from VA-API (`h264_vaapi`) or `libx264` `veryfast`, audio AAC, at
  most `media.limits.transcode` at once. ADTS AAC from MPEG-TS and RTSP is copied through
  `aac_adtstoasc`. `mm3-install.sh` puts `mm3` in the groups `render` and `video` on kiosk
  systems; the engine warns `media_group` when it is not a member.
- Remote streams (CORE-403): `https` and `rtsps` are verified against the system CA store; a
  protocol whitelist per scheme means no redirect to another scheme; credentials written in a
  source URL count as secrets for logs and diagnostic bundles.
- Media folder (CORE-404): `<state>/media/` with `files/` and `shares/`, only the listed
  extensions, never through a symbolic link.
- Media state (CORE-412): `CORE_MEDIA_CHANGED` on change, at most once a second;
  `GET /api/v1/media` (sources with `reason`, running children); a Media card on the debug
  overview, its texts in EN, IT, FR, ES, DE. The takeover is null until its step.
- `mm3 diag check` fails when media sources are configured and `ffmpeg` is missing (CORE-397);
  `mm3-install.sh` installs the package `ffmpeg`.
### Changed (spec 0.51; CORE-397, CORE-400 and CORE-412 changed)

- Core spec 0.51: an HLS viewer is a client address seen in the last 10 s, a child that exits with
  code 0 is not a failure, and 60 s of running resets the backoff (CORE-397); the first playlist
  request waits up to 10 s, then 503 with `Retry-After: 5`, and under `/media/files/` only paths
  starting with `files/` or `shares/` are files (CORE-400); `GET /api/v1/media` gives each source
  a `reason` (CORE-412). Rules 1.78.

### Added (milestone 8 step 1: config base; CORE-388..393, CORE-398, CORE-399, CORE-416, spec 0.50)

- `devices` top-level key (CORE-388): cameras (`path` under `/dev/v4l/by-id/` or
  `/dev/v4l/by-path/`) and Kinects (optional `serial`), with `owner` and `images`.
- Owner resolution at start and on every reload (CORE-389): `auto` becomes `vision` while an
  enabled `vision` instance exists, otherwise `media` for a camera and `none` for a Kinect.
- Device checks (CORE-390): duplicates by path (links resolved where the path exists) or serial,
  two Kinects without a serial included; `owner: vision` without Vision is a warning.
- `/run/mm3/devices.json` (CORE-391), written atomically with mode 0644 at start and after every
  reload, with the resolved owner and the feed socket path of each device.
- Camera images warning (CORE-392): a banner on the admin home and in the Form while a device has
  `images: true`.
- `GET /api/v1/devices` (CORE-393) and a Devices card on the debug overview; the Form edits
  `devices` like every other section, and a map of models (devices, media sources and shares,
  instance `positions`) now shows one form per entry.
- `media` top-level key (CORE-398): sources (`rtsp`, `stream`, `camera`, `files`), shares (sync or
  mount), limits, takeover defaults and `browser_audio`; settings only, no `ffmpeg` yet.
- Media checks (CORE-399): `CORE_CFG_MEDIA_URL`, `_DEVICE`, `_PATH`, `_SHARE` and the warning
  `_DELIVERY`. Rclone remotes are not checked by the engine (the installer checks them, CORE-406,
  CORE-407).
- Nine `CORE_CFG_*` codes in the code table and the engine catalog (EN, IT, FR, ES, DE), with form
  labels for every new field (CORE-310).
- `devices` and `media` are history sections after `backup` and reload-plan flags (CORE-416).

### Changed (spec 0.50; CORE-388 and CORE-398 changed)

- Core spec 0.50: a field that does not belong to the kind (a camera's `serial`, a kinect's
  `path`, a source's `url`, `device` or `path`) is `CORE_CFG_INVALID`; share names follow the
  source name pattern; `takeover.max_duration` is at least `duration`. Rules 1.77.

### Changed (spec 0.49; CORE-416 added, CORE-390, CORE-399, CORE-406, CORE-407 changed)

- Core spec 0.49: devices and media are history sections; two Kinects without a serial are a
  duplicate; the rclone remote of a share is checked by the installer when it runs the share
  (state `error`, reason `remote`), not by config validation. Rules 1.76.

### Added (milestone 8 defining; CORE-397..415, MED-01..08, MOD-100..103, spec 0.48 / rev 16)

- Core spec 0.48: the media service (9.7, CORE-397..414) and the migration of MM² media
  modules (CORE-415, milestone 9); the reference module `mm3-media` (9.4, MED-01..08);
  `MEDIA_TAKEOVER`, `MEDIA_STOP` and `CORE_MEDIA_CHANGED` in the notification registry
  (`mm3.notifications.REGISTRY`, CORE-224); ffmpeg, hls.js, cifs-utils and nfs-common in 1.2.
- Module Contract 1.0 rev 16: `ctx.media` and the capabilities `media.play`, `media.takeover`
  and `media.youtube` (§4.5, §8, MOD-100..103). **Module API:** additions only (draft 1.0).
- Spec tooling knows the `MED` prefix. Spec only: the engine does not implement the new
  requirements yet.

### Added (ENH-006; CORE-388..396, spec 0.47)

- Core spec 0.47: camera devices (6.11, CORE-388..393) and the local camera feed (9.5, CORE-394..396),
  assigned to milestone 8, plus `VISION_POSE` and `VISION_GESTURE` in the notification registry
  (`mm3.notifications.REGISTRY`, CORE-224). Spec only: the engine does not implement the new
  requirements yet.

### Added (issue log in the repository; CORE-75)

- The issue and enhancement log (`ISS-nnn`, `ENH-nnn`) moved from the claude.ai project documents into
  `docs/log/`: one file per entry in `docs/log/entries/`, with the table columns as YAML front matter,
  so an entry is edited in place instead of rewriting a whole document. `docs/log/README.md` holds how
  the log works, the entry template and the table, which `tools/log_index.py` generates; CI runs it
  with `--check` (lint job). The round 1 plan and the log history up to the move (log 2.55) are in
  `docs/log/round-1-plan.md` and `docs/log/HISTORY.md`. ISS-002 records that its fix reached main in
  the squash commit of PR #130. Tests: `tests/test_log_index.py`.

### Fixed (ISS-002; CORE-275, spec 0.46)

- Changed module settings now reach the mirror and the preview after a save. The engine already
  reconfigured or restarted the instance (CORE-210), but on `reload` `resync` the shell kept the
  element and never gave it the new public config, so only a disable and an enable showed the
  change. The shell now re-creates an element whose public config changed, with the config of the
  new `welcome`, and still keeps every element whose instance, module, version and config are
  unchanged (CORE-275, Core spec 0.46). Test: `test_core_275_changed_config_recreates_the_element`.

### Changed (milestone 7 closed; CORE-121, CORE-361, MOD-96..99, ENH-002; spec 0.45)

- `docs/spec/milestones.toml` closes milestone 7 at Core 0.45 with Module Contract 1.0 rev 15.
  CORE-121 and MOD-96..99 are built in `mm3-project/module-index` (the index CI of core
  spec 10.6: only releases with a PEP 740 attestation from the entry's repository that pass
  `mm3 module check` are listed), so they are waived here with the reason and the pull request;
  the coverage report counts all 81 requirements of the milestone as implemented and tested.

### Spec (Core 0.45: backup routes of the engine; CORE-361, ENH-002)

- No code change. CORE-361 now defines the backup routes the engine forwards to the installer, on
  the admin listener only: the local and remote lists, create, download, upload, verify,
  preview, keygen, the rclone remotes and restore. An upload is a raw body of at most 2 GiB
  (413 `CORE_API_UPLOAD_TOO_LARGE`) that a new installer route stages; a restore names it as
  `{upload}`. A restore and a change of a remote ask for the passphrase; everything else does
  not. The code PR that follows builds the routes and the Backups page and reviews the stale
  citations of CORE-361.

### Added (backup routes and the admin Backups page; CORE-361, CORE-162, CORE-161, CORE-364, CORE-365, ENH-002; spec 0.45)

- The engine forwards the backup routes of CORE-361 to the installer on the admin listener only:
  `GET /api/v1/backups`, `/backups/remote`, `POST /backups` (manual, never `pre`),
  `GET /backups/{name}` (streamed download), `POST /backups/upload`, `/backups/verify`,
  `/backups/preview`, `/backups/keygen`, `/backups/restore` and the remotes under
  `/backups/remotes`. A restore and a change of a remote ask for the passphrase and the TOTP code,
  passed on and never logged; a restore refuses `path`. New problem code
  `CORE_API_UPLOAD_TOO_LARGE` (413), also returned when the installer refuses the size.
- An upload is a raw body of at most 2 GiB, refused from `Content-Length` before any byte is read
  and streamed to the installer, never held in memory. The installer's new
  `POST /v1/backups/upload` writes it to `uploads/` in its state directory (mode 0600, temporary
  file and rename), keeps at most two files for at most 24 hours, deletes leftovers of a crash
  at start and the file a restore used when the restore ends. A restore or a verify names it as
  `{upload}`. A second upload no longer deletes one that is still being written.
- `/admin/backups` (nav link "Backups"): the list with download, verify and restore, a new
  backup with the choice of parts and encryption (secrets only when encrypted), the archives on
  the remote, an upload with progress, a new age key whose identity is shown once, the remotes
  with their test, and the status of the last scheduled backup and remote copy. A restore
  shows what it would change before it asks for the passphrase; progress follows the `installer`
  topic. Backup and restore operations are shown there like the others.
- 132 new texts (`CORE_ADMIN_BACKUPS_*`, `CORE_WEB_NAV_BACKUPS`, `CORE_API_UPLOAD_TOO_LARGE`) in en,
  it, fr, es, de.
- The citations of CORE-361 in the CLI were reviewed against spec 0.45 and updated.

### Added (admin Catalog page; CORE-120, CORE-133, CORE-132, CORE-135, ENH-002; spec 0.44)

- `/admin/catalog` (nav link "Catalog") lists the catalog with search, category filter, an
  Installed and an Updates available view (with the number of updates) and a button that
  refreshes the index through the installer. A stale index and an installer that is away are
  shown as banners (CORE-135). The detail shows publisher, releases with the check result and
  a link to the report (never fetched by the page), capabilities and grant scopes.
- Install, update, remove and restore previous open a dialog with the effect, the CLI
  alternative, a version selector when several compatible releases exist, the passphrase and
  the TOTP code. The fields are cleared before the request and never stored; a wrong passphrase
  keeps the dialog open. The remove dialog lists the instances that will be disabled (CORE-358).
- Progress follows the `installer` topic of `/admin/ws` with a read of the operation after a
  reconnect; the result ends as a done, rolled back or failed banner and the list is reloaded.
- 88 new texts (`CORE_ADMIN_CATALOG_*`, `CORE_WEB_NAV_CATALOG`) in en, it, fr, es, de.

### Added (removal disables the instances first; CORE-358, CORE-31, ENH-002; spec 0.44)

- Before a removal is forwarded to the installer, the engine saves a configuration history
  version that sets `disabled: true` on every enabled instance of the module
  (`POST /api/v1/installer/operations` with `action: remove`). The instances are kept, not
  deleted, so the configuration stays valid once the module is gone (CORE-203). The instances
  are those of every module the engine loaded from the package; package names are compared
  as PEP 503 names. An instance may be inline, in an `!include` of the
  whole `modules` list or in an `!include` of one instance file (CORE-31). Comments, order and
  secrets of the files are kept. Without an enabled instance no version is written. The version
  stays if the installer refuses the removal and `POST /api/v1/config/undo` restores the instances.
- `mm3 modules remove <name> [--yes] [--socket] [--admin-socket]` is new. It asks the running
  engine on the admin socket (CORE-311) to save the version, then sends the removal to the
  installer. Without an engine on the socket it stops with exit code 2 and changes nothing.
- The installer is checked before the version is saved: if it cannot be reached, is busy or is
  locked, the request ends with the code of CORE-356 (`CORE_API_INSTALLER_UNAVAILABLE`,
  `CORE_API_INSTALLER_BUSY`, `CORE_API_INSTALLER_LOCKED`) and nothing is written.
- `mm3.core.config.removal` holds the one rule that the engine and the CLI share
  (`module_instances`, `disable_ops`). `SocketClient.request` can send JSON requests.
- The admin pages that use the catalog routes (CORE-120, CORE-133) are still to build.

### Added (engine: catalog and installer API; CORE-356, ENH-002; spec 0.44)

- `GET /api/v1/catalog` (`q`, `category`) and `GET /api/v1/catalog/{name}` read the engine's
  copy of the index only and work from a stale one; a module with no compatible release is
  listed with `compatible` false and the reason `api`; an installed module the index does not
  list is `unlisted`; `GET /catalog/{name}` adds `publisher` and `releases` with the check result
  and its report link. `POST /api/v1/catalog/refresh` asks the installer to fetch and verify the
  index.
- `POST /api/v1/installer/operations` (install, update, remove, restore previous; admin listener
  only; always the catalog as source, passphrase on every action, TOTP when enabled),
  `GET /api/v1/installer/operations` and `/operations/{id}`. The installer's answers map to
  `CORE_API_REAUTH_FAILED`, `CORE_API_INSTALLER_LOCKED` (with `locked_until`),
  `CORE_API_INSTALLER_BUSY`, 422 `CORE_API_INVALID_REQUEST` and `CORE_API_INSTALLER_UNAVAILABLE`;
  `CORE_API_CATALOG_UNAVAILABLE` is new too (texts in the five languages). The passphrase and
  the code are never logged or sent back.
- `GET /api/v1/installer` now answers `{available, busy, locked_until}`.
- `/admin/ws` has the topic `installer` (`{status, operation}` frames): the installer is read
  every second (every 5 s while it cannot be reached) only while someone is subscribed.
- This completes CORE-356. The backup routes (CORE-361) are not part of it. The admin pages
  that use these routes (CORE-120, CORE-133) are still to build; CORE-358 follows in its own
  entry.

### Spec (Core 0.44: engine catalog and installer API; CORE-356, ENH-002)

- No code change. CORE-356 now defines what the engine's catalog and installer routes answer:
  `GET /api/v1/catalog` and `/catalog/{name}` (modules with no compatible release are listed with
  the reason; the check result comes with a report link), `POST /api/v1/catalog/refresh`,
  `GET /api/v1/installer` as `{available, busy, locked_until}`, the operation routes and object,
  the mapping of the installer's answers to the `CORE_API_*` codes, and the polling and frames of
  the `installer` topic of `/admin/ws`. The code PR that follows reviews the stale
  citations of CORE-356.

### Fixed (installer: ISS-017; CORE-355)

- The installer is free as soon as an operation reaches a final state (`done`, `failed`,
  `rolled_back`), before that state can be read. A client that polled the state and sent the
  next request at once could get 409 (`test_core_361_restore_needs_the_passphrase_except_from_root`
  failed at random). The pruning of old venvs now runs before the final `done` step.

### Tests (milestone 7: rules that had no test; CORE-50, CORE-66, CORE-122, CORE-123, CORE-126, CORE-130, CORE-349)

- No new behaviour. Tests that name the requirement: the three catalog badges and the
  refusal of any other (CORE-122), the entry's publisher is required and passed to the
  verification (CORE-123), a failed verification installs nothing and unlisted modules are
  marked in the CLI list (CORE-126), the engine reaches the installer only through its
  client (CORE-130), only the installer imports `sigstore` and `pypi-attestations`
  (CORE-349), no engine module runs `pip` or `uv` (CORE-66), the MQTT bridge talks through
  ordinary bus notifications (CORE-50).
- Not covered here because the code does not exist yet: CORE-120 and CORE-133 (admin
  installation and the details before confirmation), CORE-358 (disabling the instances
  before a removal; built later, see its own entry), CORE-121 and MOD-96..99 (module-index).

### Changed (admin: the System card style on the whole admin interface; ENH-012; spec 0.43, CORE-384..387)

- New shared module `adminui/ui.js` with the banner, the action row and the confirmation
  dialog, used by every admin page (CORE-384..387).
- Confirmation dialogs (CORE-384): Cancel has the focus, Escape and the backdrop cancel,
  the confirm button is red only where the action is destructive (reboot, replacing the
  draft). Restoring a version and replacing the draft no longer use the browser's own
  boxes.
- Form and preview dialogs (CORE-385): title, one sentence that says where the change
  lands and what is saved, the first field focused (Add module, Add page) or Close focused
  (Review changes), actions in a row at the end.
- Banners (CORE-387): four kinds, error, warning, information and done, with `role="alert"`
  for errors and `role="status"` for the rest; a done banner clears after 8 s. The editor
  error list, the layout refusals, the debug page and the extensions notices use them.
- Action rows (CORE-386): each action is a row with its effect; when it cannot run the
  button is disabled with the reason as the tooltip and next to it (restore on the current
  version, history, debug instance controls).
- 24 new catalog keys in five languages. Tests: `tests/test_admin_ui.py` (banner kinds and
  the 8 s clear under node) and new Chromium tests in `test_admin_editor.py`,
  `test_admin_layout.py` and `test_admin_debug_page.py`; selectors of existing tests moved
  from `p.alert` to `div.alert`.

### Added (admin: restart the engine or the kiosk and reboot from the admin; ENH-002; spec 0.42, CORE-382, CORE-383)

- The admin home has a System card (one row per action, with its effect): "Restart engine",
  "Restart kiosk" and "Reboot system". Each opens a confirmation dialog with Cancel focused
  and no passphrase (CORE-383). After Confirm a banner follows the effect through the
  reconnect of the admin channel (CORE-250): it ends when `started_at` is later (engine,
  reboot) or the kiosk reconnects, or after 120 s with an "effect not seen" message. The
  buttons are disabled, with the reason as the tooltip, while the installer is unreachable
  or busy.
- Engine: `POST /api/v1/system/restart-engine|restart-kiosk|reboot` on the admin listener
  only, answering 202 `{action, requested_at}`; 409 `CORE_API_INSTALLER_BUSY`, 409
  `CORE_API_NO_KIOSK` and 503 `CORE_API_INSTALLER_UNAVAILABLE` (CORE-382).
- Installer: `POST /v1/system/*` answers 202, waits 2 s, then runs `systemctl restart
  mm3.service`, `systemctl restart mm3-kiosk.service` or `systemctl reboot`, and logs the
  action with the client at warning level. While an action waits, operations and scheduled
  backups are refused (busy). The polkit rule now covers those units and
  `org.freedesktop.login1.reboot` (CORE-357, CORE-359, CORE-382).
- Minimal slice of CORE-356: the engine had no installer forwarding layer, so this adds the
  installer client and `GET /api/v1/installer` returning `{available, busy}`. The spec does
  not define that shape; it is an implementation choice recorded in ENH-002.
- 26 new catalog keys in five languages. Tests: `tests/test_admin_system.py` (routes, the
  installer side, the polkit matrix, the UI logic under node, two Chromium runs).

### Added (admin: MQTT connection indicator and the admin home status view; ENH-007; spec 0.41, CORE-381)

- Every admin page except the login has an MQTT indicator in its header: a coloured dot
  and a text label from the `mqtt` object of the status (CORE-305, CORE-339): grey "MQTT
  off" without a bridge, green "MQTT connected", amber "MQTT connecting" (not connected,
  no error), red "MQTT error" (the reason in the tooltip, with `since`), and grey "MQTT
  status unavailable" while the admin channel is closed. It follows the `status` topic of
  `/admin/ws` (CORE-304, CORE-317) through one channel per page and does not poll.
- `/admin` is now the status view that CORE-305 and CORE-378 describe, not the placeholder
  page: engine, clients, kiosk and MQTT cards, with mismatches, kiosk failures and an MQTT
  error highlighted, live from the same topic. `/admin/debug` shows the same cards (the
  code is shared in `status.js`), so its overview now has the MQTT line too.
- Ten `CORE_ADMIN_*` texts in five languages; the unused `CORE_WEB_ADMIN_PENDING` is removed.
- Tests: `test_core_381_*` in `tests/test_admin_debug_page.py` (page without inline code,
  the texts in five languages, and a Chromium test that walks the five states on the home,
  the debug page and the editor page).

### Fixed (test kit and tests: three intermittent failures; ISS-015, ISS-016; no spec change)

- `mm3.testing` virtual clock (`LoopClock`, CORE-278): a job due exactly at the end of
  `h.advance()` could be missed. The loop clock starts at the real monotonic time, so a
  time planned from the wall clock and the target of `advance(to=...)` can differ by one
  float rounding (about 1e-11 s). The scheduler then waited for that remainder, asyncio
  ran the timer at once because it is inside its clock resolution, the virtual time did
  not move, and the scheduler waited again until the settle rounds ran out. Waits shorter
  than a microsecond (one `datetime` tick) are now rounded up to one microsecond, and
  `advance()` also runs a timer up to two microseconds past its target. Nothing changes
  for a wait of a microsecond or more. This is the cause of the intermittent
  `test_prs_05_days_are_start_days` (13 failures in 190 runs before, none in 70 after).
  `test_wth_11_sent_after_every_refresh_whatever_the_view` (ISS-015) fails the same way,
  one job missed at an exact boundary, but its failure was seen only 2 times (CI and one
  local run) and was not captured, so it is expected to be fixed by this and not proved.
- `test_core_377_history_survives_a_reload_until_save_or_discard` (ISS-016) checked the
  Redo button as soon as it was drawn, before the page had restored the stored history
  (the buttons are drawn first). It now waits for the enabled button and for each step
  (9 failures in 24 runs before, none in 60 after). The page itself is unchanged.
- Test: `test_core_278_advance_runs_a_job_one_float_rounding_past_the_target`.

### Changed (admin landing; ISS-001; Core spec 0.40)

- An admin page asked for without a session answers 303 to
  `/admin/login?next=<path and query>` instead of a bare `/admin/login` (CORE-378):
  `/admin`, `/admin/config`, `/admin/debug`, `/admin/preview` and `/admin/ext/<module>`.
  The login form carries `next` in a hidden field and keeps it after a failed or locked
  attempt; a successful login answers 303 to `next` when it is a path under `/admin`, and
  to `/admin` otherwise. A `next` with a scheme or host, a backslash, a control character,
  a dot segment (also percent-encoded), a fragment, more than 1 KiB, or pointing at
  `/admin/login` or `/admin/logout` is ignored (`mm3.core.web.apps.safe_next`).
  `GET /admin/login` with a valid session answers 303 to `/admin`. API routes keep
  answering 401. No new catalog keys.
- Tests: `test_core_378_*` in `tests/test_web_security.py` and a real-browser test
  `test_core_378_browser_login_returns_to_the_page_asked_for`; the redirect assertions of
  the debug page and the preview shell now expect the `next` parameter.
- Milestone 6 is closed again at Core 0.40 (`docs/spec/milestones.toml`). The admin home
  (`/admin`) is still the placeholder page; the status view CORE-305 describes for it is
  not part of this change.

### Changed (admin editor: draft history, Save without a dialog, absent sections; ENH-004, ENH-010, ISS-008; Core spec 0.40)

- Draft history life (CORE-377). The undo and redo steps of the draft are kept in the
  browser with the draft, so a page reload keeps both. The history starts at the version
  the draft was loaded from and is cleared by Save, Discard or Open in editor (which no
  longer adds an "Open version" step); Undo is disabled at its start. Up to 500 steps; the
  browser store keeps one splice per changed file instead of whole texts, and drops the
  oldest steps first when the storage quota is reached. A stored history that does not fit
  the stored draft is dropped, never half used.
- Save writes at once with no confirmation or preview (CORE-302, CORE-377). The old
  "Save these changes?" dialog is gone; the diff and the validation records open from the
  new "Review changes" button of the save bar. Catalog keys `CORE_ADMIN_SAVE_TITLE` and
  `CORE_ADMIN_OPENED_VERSION` are removed.
- Absent sections (CORE-379). `audio`, `backup`, `pages` and the other optional top-level
  sections are no longer greyed out in the Form view; they show "Not in config, defaults
  apply", with "Add section" (`{}`, or the page `main` for `pages`) and "Remove section"
  as one draft step each. A toggle shows its schema default while the key is absent.
- Section undo (CORE-152, ISS-008). "Undo last change here" marks its step as a revert of
  the step it undoes; each press reverts the latest step of the section that is neither a
  revert nor already reverted, so repeated presses walk back through the section's steps
  instead of toggling between the last two values. The button shows the label of the step
  it would revert and is disabled when there is none; undoing a revert with Ctrl+Z makes
  its step revertable again.
- Catalogs: six new `CORE_ADMIN_*` keys in EN, IT, FR, ES and DE (compiled files and
  `messages.pot` updated). The stale `textPrefix` property of the Form view is removed.
- Tests: `test_core_377_*`, `test_core_302_review_changes_shows_the_diff_on_request`,
  `test_core_152_repeated_section_undo_walks_back_through_the_section` and
  `test_core_379_absent_sections_are_listed_and_can_be_added_and_removed`; two existing
  tests no longer click through the removed dialog.

### Fixed (admin Form view: labels in the admin language; ISS-003; Core spec 0.40)

- The Form view shows each field's schema `title` as its label and its `description` as
  its help, both read as catalog keys of the module that owns the model, then of the core
  catalog (CORE-310, Module Contract MOD-46). The path-derived keys
  `CONFIG_<PATH>_LABEL` and `_HELP` are no longer read. A missing label shows the field
  name and a missing help shows nothing. `GET /admin/i18n.json` serves the `CFG_*` keys
  of each catalog in `forms`, instead of the `CONFIG_*` keys.
- The engine's own configuration models (`engine`, `display`, `audio`, `layout`, `pages`,
  `modules`, `backup` and every nested model) now carry `title` and `description` keys
  `CFG_<SECTION>_<FIELD>` and `CFG_<SECTION>_<FIELD>_HELP` (119 fields, 238 keys), with
  texts in EN, IT, FR, ES and DE in the core catalog (compiled files and `messages.pot`
  updated). Before this the engine's sections showed bare field names in every language.
- `mm3 module check`: a config field without a `title` or a `description`, or whose key is
  not in the English catalog, is a MOD-46 warning (a SHOULD finding, CORE-310); a title
  that is not an UPPER_SNAKE key still fails the MUST. Models nested in the configuration
  and defined in the package are checked too. New `label_problems()` in
  `mm3.core.config.modelcheck` does the same on a loaded model.
- Tests: `tests/test_form_labels.py` fails CI when an engine field or a field of
  `mm3-clock`, `mm3-weather`, `mm3-presence` or `mm3-greeting` lacks a label or a help,
  or when a language lacks a key; a Chromium test checks the labels in English and Italian.

### Specification (spec sync to Core spec 0.40; Source: ISS-011)

- `docs/spec/` synced to Core spec 0.40 (Module Contract 1.0 rev 15 unchanged).
  Added: CORE-380 (`GET /api/v1/installed-modules`, the modules the engine
  loaded, with kind, label, usable and instances), assigned to milestone 6.
  Changed: CORE-374 (Add module reads CORE-380; `GET /api/v1/modules` lists
  instances, CORE-315). No code change; the code is PR C2.

### Added (admin Layout view: add module, Services lane; installed-modules route; ISS-011 completed; Core spec 0.40)

- `GET /api/v1/installed-modules` (CORE-380): the modules the engine loaded, sorted by
  `name`, each with `version`, `distribution`, `kind` (`frontend` or `backend`), `header`
  in the admin language, `usable`, `error` and the ids of its instances. A module the
  registry refused is listed with its reason. Read-only, outside `/api/v1/modules/<instance id>/`.
- "Add module" in the Layout view (CORE-374): a dialog lists the usable modules from
  CORE-380 (a refused module is shown with its reason and cannot be picked), takes a
  position preset for a frontend module and adds the instance as one draft step. The
  `id` is always written (the module name, else `<module>_2`, `<module>_3`…), also on
  existing instances of that module that had none, and the new instance is limited to
  the current page when the configuration has `pages`.
- Services lane (CORE-375, in part): instances of backend modules are shown in their
  own lane with "Delete", and are left out of the disabled list.

### Security (dependencies; core spec 10.9)

- `opentelemetry-api` 1.45.1 -> 1.45.2 in `uv.lock` (transitive; CVE-2026-126360 reported by
  the `pip-audit` job, CORE-174). No code or spec change.

### Added (admin Layout view: delete an instance, pages; ISS-011 in part, ENH-001; Core spec 0.39)

- Delete an instance (CORE-375): "Delete" on the overlay toolbar and on each entry
  of the disabled list removes the instance's whole entry as one draft step, with no
  confirmation; the step is undone like any other (CORE-151).
- Pages in the Layout view (CORE-376): "New page", "Rename page" and "Delete page"
  beside the page selector, each one draft step. New page checks the id as typed
  (CORE-230) and writes the title as a `LocalizedText` in the session language; without
  a `pages` block it creates one with `main` and the new page. Rename changes the title
  in the session language only. Delete is not offered for the last page; it removes the
  page from `pages.list`, from every instance's `pages` and `positions`, disables an
  instance left with no page and moves `default` to the first remaining page when it
  was the deleted one.
- Not in this PR: "Add module" (CORE-374) and the Services lane with its Delete
  (CORE-375). CORE-374 names `GET /api/v1/modules` as the list of installed modules,
  but CORE-315 defines that route as the running instances; no route returns installed
  modules with their label and kind. This is a spec gap, settled in its own defining
  session (Core 0.40); ISS-011 stays open until then.
- Catalog keys `CORE_ADMIN_DELETE`, `CORE_ADMIN_NEW_PAGE` and others in the five
  languages.

### Specification (spec sync to Core spec 0.39; Source: ISS-003, ISS-011, ENH-001, ENH-004, ISS-008, ISS-001, ENH-010)

- `docs/spec/` synced to Core spec 0.39 (Module Contract 1.0 rev 15 unchanged).
  Changed: CORE-152 (repeated "Undo last change here" walks back through the
  section's steps), CORE-302 (the save diff is shown on request, Save asks
  nothing) and CORE-310 (form label and help are the schema `title` and
  `description` as catalog keys, MOD-46). Added: CORE-374 (add an instance),
  CORE-375 (delete an instance), CORE-376 (pages in the Layout view), CORE-377
  (draft history life), CORE-378 (admin landing) and CORE-379 (absent sections
  in the Form view), assigned to milestone 6, which they reopen (CORE-79).
  No code change; the code follows in its own PRs.
- Test `test_core_75_log_ids_are_not_requirements`: issue-log ids (`ISS-nnn`,
  `ENH-nnn`) in a history row's `Source:` part are not read as requirement ids.

### Fixed (CI: mm3 module check venv; no spec change)

- `mm3 module check` ended with exit code 2 ("Chromium cannot be started") once
  Playwright 1.65 was released: the check's temporary venv is resolved without
  `uv.lock`, took the newest Playwright, and looked for a Chromium build that CI
  (which installs the locked 1.64.1's) does not have. The venv now pins Playwright to
  the version that runs the check (`mm3.core.modcheck.build`, CORE-281). The jobs
  "Reference module ... (CORE-289)" and `test (3.13)` failed on this.

### Fixed (admin Form view save path; ISS-012, ISS-010; no spec change)

- A Form view edit in a section or list that is not in the configuration yet
  (the mute toggle of an absent `audio`, Add on `backup.parts` or `pages.list`)
  failed with 422 `CORE_API_PATCH_FAILED` "… does not exist" and blocked Save
  (ISS-012). The Form created missing parents only below an existing section;
  the new `setOp` in `draft.js` builds one operation that creates every missing
  parent from the file root, the section included, and replaces a null section
  (`audio:` with no value). Requirements: the Form view and CORE-301
  (operations must apply to the draft's document).
- A refused save showed only the error count ("1 errors") when the record sat on
  a section itself, such as the missing position of an instance (CORE-33,
  recorded at `/modules/N`), because the Form showed errors only on fields
  (ISS-010). The Form now shows a section's own errors at its top; the save
  bar's count opens the list of the draft's errors with a link to each section;
  record messages are shown in the admin language from their `CORE_CFG_*` code
  (CORE-206: the admin texts now carry the `CORE_CFG_*` keys), in the Form, the
  YAML gutter, the record lists and the save dialog. Requirements: Validation
  while editing, CORE-35, CORE-206.
- Tests: `test_core_301_form_ops_create_missing_parents`,
  `test_core_301_form_edit_in_an_absent_section_applies`,
  `test_core_35_section_errors_shown_and_listed`,
  `test_core_206_admin_texts_carry_validation_codes`;
  `test_admin_texts_follow_the_engine_locale` now expects the `CORE_CFG_*` texts.

### Fixed (core spec 0.38: browser form posts, milestone 3 reopened)

- The login, logout and pairing forms were refused with 403 from every browser
  (CORE-253, CORE-63). With `Referrer-Policy: no-referrer` a browser sends
  `Origin: null` on a same-origin form post, and the `Origin` check refused it
  before the token was looked at; the tests missed it because they set `Origin`
  by hand. The policy is now `same-origin` on both listeners (still no referrer
  to other sites) and the browser sends the real origin. `Origin: null` stays
  refused. New tests drive the forms with a real browser
  (`tests/test_web_browser_forms.py`, `browser` marker) and keep `Origin: null`
  refused (`test_core_253_unsafe_requests_need_a_matching_origin`).
- Core spec 0.38: CORE-63 and CORE-253 changed; the `Implements:` citations of the
  files that cite them were reviewed and updated, and milestone 3 is closed again
  at core 0.38 in `docs/spec/milestones.toml`.

### Added (milestone 7, PR 5: backup and restore, installer side and CLI)

- Backups (core spec 0.37, section 10.8; CORE-160, CORE-161, CORE-362): archives
  `<hostname>-<YYYYmmdd-HHMMSS>-<kind>.mm3backup` (gzip tar with `manifest.json`,
  `config/`, `data/<instance>/`, `modules.json`, `secrets/`) and an unencrypted
  sidecar `.mm3backup.json`, in `/var/lib/mm3/backups/` (0600); caches are
  excluded and links are never followed. `secrets` go only into an encrypted
  archive, otherwise the part is left out and the manifest says so.
- Encryption with age (CORE-161, CORE-364), the Debian package `age`: a public key
  for scheduled backups, a passphrase (through a pseudo-terminal, never on a command
  line) for manual ones; `mm3 backup keygen` shows the identity once and keeps it
  as `backup.key` (0600) only with `--keep`. A restore takes the kept identity or
  the key sent with the request; it exists only in the operation's private directory.
- Scheduler (CORE-360, CORE-244): the installer reads the `backup` block and
  `engine.timezone` over the admin socket at start and every hour (the files in
  `/etc/mm3` when the engine does not answer), runs at `backup.schedule`, catches up
  within minutes of its start when the last scheduled backup is over 24 h old,
  applies `keep` (daily, weekly and monthly, scheduled backups only) and keeps the
  last result for `GET /v1/status`.
- Remotes (CORE-365), the Debian package `rclone`: `sftp`, `smb`, `webdav` and `s3`
  in `/var/lib/mm3-install/rclone.conf` (0600), secrets returned as `!masked` and
  kept when sent back, passwords obscured through stdin; after a scheduled backup the
  archive and then its sidecar are copied and `keep` is applied there; a failure is
  recorded and does not fail the local backup.
- Restore (CORE-162, CORE-163, CORE-164, CORE-165, CORE-363): decrypt, verify the
  manifest checksums, check compatibility (a newer engine or another Module API
  major is refused, another host warns), preview part by part, take the pre-restore
  backup, build the new venv when `modules` is selected, stop `mm3.service`, write
  `secrets`, `data` and `config` through descriptor-relative writes that never follow
  a link (CORE-373), switch the venv, start the engine and check it; when it is not
  READY within 60 s or `mm3 diag check` fails the plain pre-restore copy is put back.
  One module instance can be restored from `config` (its section only) and `data`.
  The engine records the restored files as a history version with origin `restore`
  (marker `restore.json` in its state directory).
- Pre-operation backups (CORE-366): a backup of kind `pre` before every install,
  update, removal, restore previous and restore, the last 3 kept; when it fails the
  operation does not start. `mm3-install.sh` asks the installer for one before an
  upgrade (`mm3 backup create --pre`, root) and installs `age` and `rclone`.
- Installer routes (CORE-361, CORE-364, CORE-365): `GET|POST /v1/backups`,
  `GET /v1/backups/{name}` (download), `GET /v1/backups/remote`, `POST
  /v1/backups/verify|preview|restore|keygen`, and `GET /v1/backups/remotes`,
  `GET|PUT|DELETE /v1/backups/remotes/{name}` and `POST .../test`. A restore and
  changes to a remote re-authenticate, except a restore from a root peer; a backup by
  file path and `pre` are root-only.
- CLI `mm3 backup create|list|verify|restore|keygen` (CORE-361); `mm3 backup restore`
  shows the preview and asks before it starts. The engine's `/api/v1/backups*` routes
  (with the 2 GiB upload limit) and the Backups admin page follow with the
  forwarding of CORE-356.

### Added (milestone 7, PR 4a: installer service and catalog client)

- Installer service `mm3 installer serve` (core spec 0.35, sections 10.6 and 10.7):
  HTTP/JSON on `/run/mm3-install/installer.sock` (mode 0660, group `mm3`) with
  `GET /v1/status`, `POST /v1/catalog/refresh`, `POST|GET /v1/operations`,
  `GET /v1/operations/{id}[/events]` and the root-only `/v1/auth/*` routes;
  peers are identified with `SO_PEERCRED`; 403 on failed re-authentication,
  423 when locked, 409 while an operation runs (CORE-355, CORE-353, CORE-369).
- Catalog index (CORE-350, CORE-351): `mm3.core.catalog` (schema 1, compatibility
  with the running Module API, search, the engine's copy); only the installer
  verifies the Sigstore signature, keeps the highest accepted `serial` in its own
  state, refuses expired or rolled-back indexes, refreshes daily and on demand,
  and writes the engine's copy with `meta.json`.
- Verification adapter `mm3.installer.verify` (CORE-349, CORE-352): the index
  bundle with the fixed module-index identity, and PEP 740 provenance from PyPI's
  Integrity API against the entry's Trusted Publisher; the wheel's SHA-256 is
  checked against the index first. New extra `mm3-core[installer]` with
  `sigstore` 4.x and `pypi-attestations` 0.0.31.
- Downloads `mm3.installer.fetch` on the standard library: HTTPS only, three
  hosts, redirects checked, size caps (CORE-370).
- Operations (CORE-130..136, CORE-354, CORE-367): install, update, remove and
  restore previous, one at a time, kept with their steps; staged venvs built with
  `python3 -m venv` and `pip --only-binary=:all:` as `mm3-build` through `setpriv`
  without capabilities, frozen pins, conflicts listed, packages only the module
  used removed, venv made read-only before the import test; restart through
  systemd, READY within 60 s and `mm3 diag check` (as `mm3`) or rollback; the last
  3 venvs kept; every operation logged with client, package, versions and outcome.
- Passphrase (argon2) and TOTP (pyotp) re-authentication with a 15-minute lock
  after 5 failures; `sudo mm3 installer passphrase` and `totp enable|disable`
  (CORE-132, CORE-369).
- CLI `mm3 modules search|info|list|refresh|install|update|restore-previous` and
  `mm3 installer serve|unit|polkit|passphrase|totp|status` (CORE-353, CORE-124,
  CORE-125, CORE-133).
- `mm3-installer.service` and its polkit rule ship in the package (CORE-357,
  CORE-359, CORE-368); `mm3-install.sh` creates `mm3-install` and `mm3-build`,
  builds the installer venv `/opt/mm3/installer`, installs `polkitd`, the unit
  and the rule, and gives `/opt/mm3/venvs` to the installer.
- `mm3 module check --wheel <file> --only static` checks a built wheel without
  its source tree or `uv`, reporting project-file checks as skipped; the
  installer runs it on the verified wheel in the read-only staged venv (CORE-371).
- While `mm3-core` is 0.x, staged rebuilds install pins and the module wheel with
  `--no-deps`, then the wheel's other requirements with the pins as constraints
  (CORE-372).
- Security review fixes (core spec 0.37): writes into the engine's directories
  never follow a symlink and set owner and mode on the open file (CORE-373);
  build children no longer change the bounding set, which needs CAP_SETPCAP, and a
  surviving `mm3-build` process stops the operation; venvs are listed from their
  `.dist-info` metadata with validated names and versions instead of running their
  interpreter, so `GET /v1/status` starts no children; module requirements with a
  direct URL are refused (CORE-367); TOTP codes are single-use and root commands
  name the installer venv's `mm3` (CORE-369); `Content-Length` must be ASCII
  digits and at most 32 connections are served at once; the installer unit is
  hardened and its own venv is read-only to it.

### Added (milestone 7, PR 3: presence and power)

- Power hooks (core spec 0.34, section 9.2, CORE-52, CORE-53): `ctx.system` with
  `request_suspend(reason)`, `request_resume(reason)` and `set_display_power(on)`,
  for modules that declare the capability `system.power`; without it the
  property raises `PermissionError` and the use is listed in `h.denied`. The
  engine is suspended only while at least one instance asked to suspend and none
  holds power; the last request of an instance wins, a stopped or failed
  instance drops its request, and transitions run in the order they were asked
  for.
- Display power (CORE-346): the kiosk agent gets a `power {on}` frame (and the
  last value at every connect), runs `wlr-randr --off` or applies the display
  block again, and reports `outputs`; `set_display_power` returns when that
  report arrives and raises `TransientError` (`CORE_DISPLAY_NO_KIOSK`,
  `CORE_DISPLAY_TIMEOUT`, `CORE_DISPLAY_UNAVAILABLE`) when it cannot. The state
  is never saved. `CORE_DISPLAY_CHANGED` carries `power` (`on` or `off`) and is
  emitted on every change. Core catalog keys for the three errors in EN, IT, FR,
  ES and DE.
- `CORE_INTERACTION {kind: "touch"}` (CORE-347): broadcast at most once a second
  for a touch on the kiosk shell; other roles do not count.
- Installer (CORE-348): `ddcutil` and `cec-utils`, and the mirror user in the
  groups `i2c` and `dialout`.
- `mm3 diag check` reports the display driver of the presence module from its
  health fields `driver` and `driver_error` (CORE-312, PRS-06).
- `tools/trace_report.py` also reads the `Implements` comments of `install/*.sh`,
  so CORE-348 counts as implemented.
- Test kit: `h.system` (`FakeSystem`: display calls and an `error` to raise) and
  `h.suspended`; the harness runs the engine's real power service.
- `mm3-presence` 1.0.0 (PRS-01..08), a backend-only reference module in
  `modules/mm3-presence`: sensors by MQTT topic or `PRESENCE_DETECTED` and
  `PRESENCE_CLEARED`, touches, the states `on`, `idle` and `off`, schedule
  windows with days and clock changes, display drivers `kiosk`, `ddcutil` and
  `cec` plus plug-ins on `mm3_presence.display`, `PRESENCE_STATE`, the routes
  `GET state` and `POST display/on|off`, and Home Assistant entities. It joins
  `mm3-modules-default`, the `modules` CI matrix and the release build.

### Added (milestone 7, PR 2: pages)

- Pages out of the cycle (core spec 0.34, section 6.9, CORE-333): `cycle: false`
  on a `pages.list` entry. Rotation, `PAGE_NEXT`, `PAGE_PREV` and swipes skip
  such a page; from it they go to the next or previous page of the cycle in
  list order; rotation does not run on it; with no page in the cycle they
  change nothing. `GET /api/v1/pages` gives every page's `cycle` flag.
- Timed jump (CORE-334): `after: {time, page?}` changes to `page` (default
  `pages.default`) once the page has been current for `time`. An unknown target
  is `CORE_CFG_PAGE_UNKNOWN`, the entry's own page (also through the default)
  is `CORE_CFG_INVALID`. A kiosk touch restarts the count; rotation does not
  run on a page with `after`.
- Idle return (CORE-335): `pages.return_after` returns to the default page
  after that long without interaction, on pages where neither rotation nor a
  timed jump runs. Rotation, timed jumps and idle returns are not interactions.
- Pause holds (CORE-336): `PAGE_PAUSE {for?}` and `PAGE_RESUME`, `POST
  /api/v1/pages/pause` (`{for?}`) and `/pages/resume`, and the Home Assistant
  switch `pages_paused` (holder `mqtt`, CORE-343). One hold per holder (the
  sending instance, `rest` or `mqtt`); a hold ends on a resume, after `for`, or
  when the holding instance stops. While one is active rotation, timed jumps and
  idle return make no change; the last one ending, and `CORE_RESUME`, restart
  their counts from zero. `GET /api/v1/pages` carries `paused`.
- `CORE_PAGE_CHANGED` carries `previous` (null with reason `start`) and the
  reasons `after` and `idle` (CORE-337).
- Core catalog key `CORE_HA_PAGES_PAUSED` in EN, IT, FR, ES and DE.

### Added (milestone 7, PR 1: MQTT bridge and Home Assistant)

- MQTT bridge (core spec 0.34, section 9.3). `engine.mqtt` is validated and
  defaulted (CORE-338: `mqtt[s]://` URL, `base_topic`, `discovery_prefix`,
  derived `node_id`, secret `password`, unreadable `ca` reported as
  `CORE_CFG_FILE_UNREADABLE`). One aiomqtt connection behind an adapter keeps
  the Last Will `offline` on `<base>/state`, says `online` after every connect,
  reconnects with an exponential backoff (1 s to 60 s, +-20 %), restores the
  subscriptions, reports `{connected, since, error}` in `GET /api/v1/status` and
  in `mm3 diag check`, and never logs credentials (CORE-339, CORE-312). The
  block reloads live with the configuration (CORE-339).
- Inbound `<base>/notify/<NAME>` messages become notifications with source
  `mqtt` when `mqtt.import` allows the name; refused messages are counted and
  warned about once a minute per name (CORE-340). Names in `mqtt.export` go
  out as envelopes on `<base>/event/<NAME>`, never an echo of source `mqtt`,
  and are dropped while the broker is away (CORE-341).
- `ctx.mqtt` (CORE-100, CORE-342): `ConfigurationError` without a bridge,
  `TransientError` when publishing while disconnected, `PermissionError` on
  engine topics; subscriptions made while disconnected follow the connection.
- Home Assistant discovery (CORE-343): the engine's entities (reload, page,
  kiosk volume and mute, `<id>_visible`, `<id>_enabled`) as retained configs
  with the availability of `<base>/state`, commands on `.../set`, and a full
  republish after a reconnect, on `<prefix>/status` `online` and on a locale
  change. Modules register their own entities with `ctx.mqtt.ha_entity()`
  under the capability `ha.discovery` (CORE-344), and stale entities are
  cleared by the rules of CORE-345 using `<state>/mqtt/discovery.json`.
- `PageService.listeners` and `KioskAgents.audio_listeners` tell the bridge
  when a page, a visibility or the audio state changes.
- Test kit: `FakeHaEntity` and the MQTT extensions of `FakeMqtt`.
- Not in this PR: the entity `pages_paused` and the hold `mqtt` (CORE-343,
  CORE-336) wait for the pause service.

### Changed (core spec 0.34, Module Contract unchanged at 1.0 rev 15)

- Spec sync for core spec 0.33 and 0.34, assigned to milestone 7. Catalog and
  installer: added CORE-349 (sigstore and pypi-attestations in the extra
  `mm3-core[installer]`), CORE-350 (signed index format), CORE-351 (cache,
  `serial` and `expires`), CORE-352 (wheel SHA-256 and PEP 740 provenance),
  CORE-353 (CLI through the installer socket, unlisted packages only from
  root), CORE-354 (staged venv with frozen constraints), CORE-355 (installer
  protocol), CORE-356 (engine catalog and installer API, new problem codes,
  `/admin/ws` topic `installer`), CORE-357 (installer unit, polkit rule,
  READY check) and CORE-358 (removal disables instances first). Backup and
  restore: added CORE-359 (installer capabilities), CORE-360 (scheduled
  backups in the installer; `mm3-backup.timer` dropped), CORE-361 (backup
  routes, `CORE_API_UPLOAD_TOO_LARGE`), CORE-362 (archive layout and sidecar),
  CORE-363 (restore order), CORE-364 (age keys), CORE-365 (rclone remotes)
  and CORE-366 (pre-operation backups). They extend existing requirements
  without changing them, so no closed milestone is reopened.

### Changed (core spec 0.32, Module Contract 1.0 rev 15)

- Spec sync for core spec 0.31 and 0.32 and Module Contract 1.0 rev 14 and
  rev 15, all assigned to milestone 7. MQTT bridge and Home Assistant: added
  CORE-338 (bridge configuration), CORE-339 (connection, Last Will, status),
  CORE-340 (inbound notifications through `mqtt.import`), CORE-341 (outbound
  envelopes), CORE-342 (`ctx.mqtt` without a bridge or connection), CORE-343
  (discovery and engine entities), CORE-344 (`ctx.mqtt.ha_entity()`) and
  CORE-345 (stale entities). Presence and power: added CORE-346
  (`ctx.system.set_display_power()` and the kiosk agent `power` frame),
  CORE-347 (`CORE_INTERACTION` on touch), CORE-348 (`ddcutil`, `cec-utils`,
  groups `i2c` and `dialout`) and PRS-01..08 (the reference module
  `mm3-presence`). The new requirements extend existing ones without changing
  them, so no closed milestone is reopened.
- The spec tools know the requirement prefix `PRS` (CORE-75, CORE-77, CORE-78).

### Changed (core spec 0.30)

- Spec sync for core spec 0.30 (Module Contract unchanged at 1.0 rev 13):
  pages cover MMM-pages and MMM-ProfileSwitcher, a profile being a page.
  Added CORE-333 (pages out of the cycle), CORE-334 (timed jump to a page),
  CORE-335 (idle return to default), CORE-336 (pause holds with
  `PAGE_PAUSE`, `PAGE_RESUME` and `POST /api/v1/pages/pause|resume`) and
  CORE-337 (`previous` and the reasons `after` and `idle` in
  `CORE_PAGE_CHANGED`), assigned to milestone 7. They extend CORE-230..237
  without changing them, so no closed milestone is reopened.
- `PAGE_PAUSE` (`{for?}`, 1 to 3600 s) and `PAGE_RESUME` (`{}`) are in the
  notification registry, as in the 5.5 table (CORE-224); the engine handles
  pause holds in milestone 7 (CORE-336).

### Fixed (weekly canary, issues #54, #55, #56)

- The test configuration sets `asyncio_default_fixture_loop_scope`
  (`function`): pytest-asyncio 1.4 warns when it is unset, and the suite
  turns deprecation warnings into errors (newest-allowed, next-python).
  Modules made by `mm3 new-module` get the same setting (CORE-281).
- The newest-available canary installs `mm3-build` from the repository
  instead of looking it up on the index (CORE-176, CORE-280).

### Added (milestone 4 coverage, issue #39)

- `mm3 module check` warns about client message types that are not lower
  snake case in `ctx.send_to_client()` and `this.send()` (MOD-44, a SHOULD).
- One test per row of the module check, named after its id, and new
  defects for MOD-07 (wheel tag), MOD-08 (README without a configuration
  example), MOD-41 (declared notification missing from the README),
  MOD-44 and MOD-71 (scoped capability without a README grant); browser
  harness tests for MOD-54 (media in the preview) and MOD-84 (info logs
  on every refresh); the review rows MOD-53, MOD-64, MOD-65, MOD-81 and
  MOD-82 each have a test.
- MOD-66: the lookup of locale packages cites the Contract and has a test
  (a package overrides nothing the module ships). MOD-73 is waived (API
  policy). Milestone 4 is complete at core 0.29 / contract 1.0 rev 13.

### Added (milestone 6, part D3: admin extensions)

- Module routes (CORE-332, MOD-74): `ctx.router` with the capability
  `api.routes`, served under `/api/v1/modules/<instance id>/` on the admin
  listener and the admin socket with the API's authentication, from the end
  of `start()` until stop (503 otherwise); the engine's own instance
  sub-paths win; handler budget with 504 and the CORE-193 count; an
  exception answers 500 and the instance keeps running. New
  `mm3.core.web.modroutes`.
- Admin extension pages (CORE-102, CORE-329): `/admin/ext/<module>` for a
  module with `admin_element`, `admin.extension` and an instance, linked in
  the admin header with its `HEADER` text; its files under
  `/admin/ext/<module>/<version>/`, and the admin scripts never through the
  shell's `/modules/` route. New `mm3.core.web.extensions` and
  `adminui/ext.js`.
- The admin element gets `api`, `module`, `instances`, `locale` and `t()`
  (CORE-330); the API helper covers `/api/v1` with the admin session and
  `api.module(id)` (CORE-331). `/core/mm3.js` also exports `render` (MOD-38).
- The Form view lists an instance's capabilities (`x-mm3-capabilities` in
  `/config/schema`, MOD-71).
- `mm3 module check`: MOD-38 (admin element, scripts in the wheel, the
  capability, one define with its tag, imports) and MOD-39 (no network of
  its own) for admin scripts; the test kit adds `h.route()` and the MOD-74
  check (no engine sub-paths, answers within budget, no secrets).
- Milestone 6 is done (`milestones.toml`).

### Changed (core spec 0.29, Module Contract 1.0 rev 13)

- Spec sync for core spec 0.29 and Module Contract 1.0 rev 13, the admin
  extensions of milestone 6: CORE-329..332, MOD-38, MOD-39 and MOD-74 added.

### Added (repository tooling)

- Every workflow job has `timeout-minutes` (10 to 30 minutes instead of
  GitHub's 6 h), and pytest prints the stack of every thread for a test still
  running after 120 s (`faulthandler_timeout`), so a hung test fails CI quickly
  and shows where it stopped (seen once on `test_hot_reload.py`, CORE-208).
- **Restack** workflow and `tools/restack.py` (CORE-75): after a pull request
  is merged with squash, the open PRs stacked on it are rebased onto main
  with `git rebase --onto`, `docs/spec/coverage.md` conflicts are resolved by
  regenerating it, other conflicts are reported on the PR, and the branch is
  pushed with `--force-with-lease`. Needs the `RESTACK_TOKEN` secret
  (CONTRIBUTING.md, *Stacked pull requests*).

### Added (milestone 6, part D2: Layout view)

- Layout tab of `/admin/config` (CORE-97, CORE-322): the real shell in preview
  mode in an iframe at the engine's viewport, scaled to the window, with an
  in-house editing overlay: drag, corner resize, snapping to cells, red and
  refused on overlap or outside the grid, presets and stacks (CORE-326).
  Every edit is one draft step shared with the Form and YAML views
  (CORE-34, CORE-151, CORE-152).
- Page selector (CORE-242); edits write `positions.<page>`, `position` or a
  new override by the rule of CORE-327. Instance toolbar with the size in
  cells and pixels (CORE-142), z, preset, appearance fields, Enable/Disable
  (CORE-240), Edit in Form, and Rescale proportionally (CORE-94, CORE-328).
- `/config/patch` and `/config/validate` return `layout`, the CORE-274 object
  of the draft at the kiosk's applied mode, also when the draft has errors
  (CORE-301, CORE-302). `layout_object` moved to `mm3.core.config.layout`.
- The preview shell takes the draft layout and the selected page from its
  parent page with `postMessage`, keeps that page, and shows draft instances
  without an element as empty cells (CORE-325); `MirrorElement.preview` is
  true there (CORE-98).
- Show on mirror (CORE-307): `POST /api/v1/preview/mirror` (204) sends the
  draft to the kiosk shell only; `DELETE`, a reload, 60 s after the last push
  or the pushing session's last `/admin/ws` channel closing bring the running
  layout back; 409 `CORE_API_NO_KIOSK` without a kiosk. New
  `mm3.core.web.mirrorpreview`.
- `/admin/config` sends `frame-src 'self'` so it can frame the preview
  (CORE-63).
- `tools/check_spec_refs.py` and `tools/trace_report.py` now also read the
  admin UI files (`adminui/*.js`, `adminui/*.css`).

### Changed (core spec 0.28)

- Spec sync for core spec 0.28, the Layout view of milestone 6:
  CORE-325..328 added; CORE-63, CORE-301, CORE-302 and CORE-307 changed;
  gridstack.js dropped from the approved stack (spec 1.2) in favour of an
  in-house editing overlay (CORE-326).

### Added (milestone 6, part D1: editor views)

- `/admin/config` now has Form, YAML and History tabs over one draft kept in
  the browser (`mm3.admin.draft`, keyed by the base ETag), with Undo/Redo
  named after the step, section undo as a minimal JSON Patch, a save bar and a
  save dialog showing the records and the diff (CORE-151, CORE-152, CORE-301).
- Form view `<mm3-schema-form>` generated from the config schema: enums,
  choices, objects in `ui` order and groups, lists, mappings, LocalizedText,
  secrets (kept, change, or `!env`) and tagged nodes; texts from each
  module's `CONFIG_*` catalog (CORE-37).
- YAML view on CodeMirror 6, bundled once with esbuild
  (`tools/vendor_codemirror.mjs`, pinned versions and hashes in
  `vendor/vendor.json`), served only on the admin listener at
  `/admin/static/codemirror.js` and mounted in a shadow root for the CSP;
  syntax errors at once, server records through `validate` 300 ms after the
  last key, in-house schema completion (CORE-323).
- `GET /api/v1/config/history/{version}` returns that version's files for
  "Open in editor" (CORE-324); `/config/patch` also returns `documents`, the
  draft as data (CORE-301).
- `PUT /api/v1/session/locale` sets the admin language for the session, and
  `/admin/i18n.json` answers in it with the language list and form texts; a
  language picker in the admin header (CORE-309).

### Fixed

- Milestone 3 closed again at core 0.29 (issue #76): the CORE-63 change of
  core 0.28 (`frame-src 'self'` on `/admin/config`) was implemented in #74;
  the remaining citations were reviewed and updated.
- Test `test_core_62_tokens_are_hashed_named_and_private` (CORE-62) took the
  token secret as the text after the last `_`, which the base64url secret
  may contain; it now takes everything after the second `_` (random flake).
- Config schema: a module's `config` is now `{"$ref": ...}` instead of a bare
  string (CORE-37).

### Added (milestone 6, part C: config API and history)

- `GET /api/v1/config` (CORE-301): the config set as `{etag, version, files}`
  with roles `main`, `include`, `widget_template` and `custom_css`, the ETag in
  the header (CORE-300) and secret values as `!masked` while `!env` stays as
  written (CORE-303). New module `mm3.core.config.editor`.
- `GET /api/v1/config/schema`: one JSON Schema from the engine model and every
  installed module, `!env` and `!include` in the JSON form of CORE-320, region
  presets as an enum and `ui` hints passed through (CORE-37,
  `mm3.core.config.schema`).
- `POST /api/v1/config/patch` (CORE-301, CORE-320): YAML edits and JSON Patch
  (`add`, `remove`, `replace`, `move`, `copy`, `test`), in-house in the YAML
  adapter on the ruamel nodes, so comments, key order and indentation
  survive; tagged nodes as one-key objects. Returns the changed files and the
  full records (CORE-35); writes nothing.
- `POST /api/v1/config/validate` and `PUT /api/v1/config` (CORE-302,
  CORE-36): the engine's own validation in a scratch copy, the diff per
  section with secrets masked, save with `If-Match` as a new history version
  (origin `admin`, author the client address); `!masked` keeps the base value
  and is refused where there is none (CORE-303). 428 without `If-Match`, 409
  `CORE_API_CONFLICT` or `CORE_API_DISK_INVALID` with the current files
  (CORE-300).
- History API (CORE-321, CORE-299): paged list with `current`, diff of two
  versions as a whole or per section, undo, redo and restore with `If-Match`
  returning `{version, etag}`, 409 `CORE_API_NOTHING_TO_DO`, 422 when the target
  does not validate, 404 for unknown versions and sections.
- `/admin/config` with the History tab (CORE-322): versions with origin, author
  and changed sections, compare two versions, Restore all and Restore section
  after a confirmation that shows the diff, Undo last save and Redo. The admin
  header now links Configuration and Debug.

### Added (milestone 6, part B2: debug page)

- Admin debug page `/admin/debug` (core spec 10.5), Web Components with
  lit-html and no inline script or style (CORE-63): an overview with engine,
  displays and kiosk status, mismatches and failures highlighted (CORE-305),
  one row per instance with state, restarts and last error, and on opening it
  the traceback, health record, recent logs, log-level control (CORE-112) and
  restart; recent browser errors (CORE-114); live logs with the filters of
  CORE-316, pause and download as JSON lines; the bus inspector with name and
  source filters, search, pause, payload viewer, follow trace and re-send
  through `/api/v1/notify`, subscribed only while its tab is open (CORE-115);
  a "Download diagnostics" link to the bundle (CORE-318).
- Admin page files under `/admin/static/` and their texts at
  `/admin/i18n.json` (`CORE_ADMIN_*`, `CORE_API_*`, `CORE_WEB_*` in the engine
  locale), on the admin listener only; 57 `CORE_ADMIN_*` keys in EN, IT, FR,
  ES and DE.

### Changed (milestone 6, part B2)

- CORE-114: element errors (render, `onMessage`, `onNotification`) now carry
  the stack and the user agent; the engine logs at most 10 browser log frames
  per instance and minute and notes the first one over the limit as
  `browser_log_limited`.

### Added (milestone 6, part B1: diagnostics, server side)

- Admin channel `/admin/ws` (CORE-304): admin session and matching `Origin`,
  else close 4403; topics `status` (at subscribe and on change, compared once
  a second, `uptime_s` ignored, CORE-317), `logs` (newest 200 matching
  records, then live) and `bus`; `dropped` when an admin falls behind its
  queue of 1 000 frames per topic. Invalid subscribes are ignored and logged
  (CORE-316). New module `mm3.core.web.adminws`.
- Bus inspector `mm3.core.diag.inspector` (CORE-115): captures notifications,
  requests, replies and shell WebSocket frames only while a `bus`
  subscription is open, last 1 000 entries; secret values of the running
  configuration become `!masked` (CORE-319, `mm3.core.diag.secrets`).
- `GET /api/v1/debug/logs`: ring buffer newest first, `limit` and `before`
  paging, the filters of CORE-316, JSON lines with
  `Accept: application/x-ndjson` (CORE-299).
- Diagnostic bundle (CORE-318, CORE-116): `GET /api/v1/debug/bundle` and
  `mm3 diag bundle` write `manifest.json`, `packages.txt`, the redacted
  `config/`, `logs.jsonl`, `health.json`, `browser-errors.jsonl` and
  `kiosk.json`; secrets in YAML become `!masked ''` and are listed as
  `{file, path}`. Without the engine the CLI builds a partial bundle with
  `journal.txt`.
- Local admin socket `/run/mm3/admin.sock` (CORE-311): the REST API without
  token, mode 0660, group `mm3`, removed at stop; the engine runs on without it
  when it cannot be created.
- `mm3 diag check` (CORE-312): config, socket, listener ports, kiosk agent,
  audio output, MQTT broker (skip), display driver (skip), locale catalogs;
  exit 0, 1 or 2.

### Added (milestone 6, part A: REST base)

- Admin REST API under `/api/v1` on the admin listener (core spec 9.1), new
  module `mm3.core.web.api`, mounted by `mm3 run` through `ApiServices`:
  `GET /status` (CORE-305), `GET /modules` and `/modules/{id}/health`
  (CORE-315, CORE-113), idempotent `show`, `hide`, `restart`, `stop` and
  `start` (CORE-315, CORE-240), `GET /kiosk` and `PUT /kiosk/audio` (CORE-306),
  `POST /notify` (CORE-308), `POST /request` with `{payload}`, error replies as
  200 and 504 on timeout (CORE-314, CORE-223), `GET /pages` and
  `POST /pages/{id}|next|prev` (CORE-236), `POST /shell/reload`, and the
  schema at `GET /api/openapi.json` with admin authentication only (CORE-313).
- Errors of every `/api` route, including 401, 403, unknown routes and body
  validation, are `application/problem+json` with the `CORE_API_*` codes of
  CORE-298 (`mm3.core.web.problems`); the codes have core catalog texts in EN,
  IT, FR, ES and DE. Routes answer 503 `CORE_API_UNAVAILABLE` while the engine
  is starting or stopping.
- Run-time log levels (CORE-112): `PUT /modules/{id}/log-level`,
  `GET /debug/log-level` and `PUT /debug/log-level/{logger}` with `{level}`
  (null for the default). The logging adapter filters records per instance
  and per engine subsystem; a level more verbose than the default expires
  after `debug_ttl`.
- Runtime stop and start in the supervisor (`runtime_stop()`,
  `runtime_start()`): state STOPPED with reason `runtime`, not saved, cleared
  by a configuration change to the instance (CORE-240).
- Shells record address, user agent and connect time for `clients`;
  `KioskStatus.api_object()` builds the kiosk object of CORE-305.

### Added (milestone 3, spec-change issue #42)

- Kiosk audio (CORE-295, CORE-296; core spec 0.21): the agent lists outputs
  and cards with `pactl --format=json list sinks|cards`, classifies each output
  as `hdmi`, `analog`, `usb`, `bluetooth` or `other` from its card and port,
  and applies the `audio` block live with `pactl set-default-sink`,
  `set-sink-volume` and `set-sink-mute`, without restarting Chromium. An output
  that belongs to an inactive card profile (HDMI and analog of one HDA card) is
  reached with `pactl set-card-profile` first. A missing or unavailable output
  keeps the current default and is reported with `matched: false`. New module
  `mm3.core.kiosk.audio` (parsing and the choice, no processes).
- Agent protocol (CORE-277): the agent sends `sinks` after `hello` and after
  every change, also when an output appears, disappears or changes availability
  (checked every 10 s with the display outputs); `failure` frames carry an
  `area` (`display` or `audio`; absent means display). When the PipeWire server
  cannot be reached the agent sends an empty `sinks` and an audio failure and
  the kiosk keeps running (CORE-295).
- Engine (CORE-277, CORE-296): `KioskAgents` sends the `audio` block at connect
  and on hot reload (`ReloadPlan.audio`), records `sinks` in `KioskStatus`
  (`sinks`, `audio`, `audio_mismatch`, `audio_failure`) and offers
  `set_runtime_audio()` for run-time volume and mute, which are never written to
  the configuration or its history and are dropped on reconnect and on a changed
  block. Without an `audio` block the kiosk keeps PipeWire's own output, volume
  and mute.
- Chromium (CORE-270, CORE-297): `--enable-features=AcceleratedVideoDecodeLinuxGL`
  for VA-API video decoding on Wayland, best effort.

### Changed (milestone 3, spec-change issue #42)

- CSP (CORE-63): `media-src 'self'` after `img-src`, on both listeners.
  The `Permissions-Policy` now also denies `bluetooth`, as CORE-63 lists it.
- The kiosk agent closes its WebSocket when it stops, so the engine sees the
  disconnect at once instead of at process exit (CORE-268).
- Citations reviewed against 0.21 in the web security files, the kiosk package,
  `mm3 kiosk` and `mm3-kiosk.service` (CORE-63, CORE-143, CORE-270, CORE-277).
  CORE-246 and CORE-268 were already implemented and tested; no code change.

### Added (milestone 2, spec-change issue #41)

- Configuration `audio` block (CORE-201, CORE-296; core spec 0.21): the
  optional top-level key `audio` with `sink` (`auto`, `hdmi`, `analog` or the
  name of an output; default `auto`), `volume` (0 to 100, unset by default)
  and `mute` (default `false`). Without the block `MirrorConfig.audio` is
  `None` and the kiosk keeps PipeWire's output and volume; an output name the
  kiosk does not report is not a configuration error. The block is validated
  with the existing `CORE_CFG_*` codes (`RANGE`, `LENGTH`, `UNKNOWN_KEY`).
- `audio` is a history section (CORE-216, CORE-155): it shows in the changed
  sections of a version and in `mm3 config diff`, and can be restored alone
  with `mm3 config restore --section audio`, also when it lives in an included
  file. `TOP_SECTIONS` now lives in `mm3.core.config.diff` and the history
  imports it, so the two cannot drift apart.
- Hot reload (CORE-296): `ReloadPlan.audio` is set when the block changed,
  appeared or went away. It restarts no instance and re-lays out no shell; the
  kiosk agent applies it live in milestone 3.
- CORE-211 (core spec 0.19, `engine.location`) was already implemented with
  milestone 5; the issue is closed with the rest after a review of the code
  and of `test_core_211_*` and `test_core_288_*`.
- Citations reviewed against 0.21 in `config/model.py`, `config/diff.py`,
  `config/history.py`, `config/assets.py` and `settings.py`.

### Fixed

- Test kit (CORE-278): a thread-pool job of `mm3.testing` now stays pending
  until its result has reached the waiting coroutine, so `settle()` and
  `h.advance()` no longer return early between a finished thread and the
  wake-up it queues (the flaky WTH-11 refresh count in CI).
- `mm3-weather` browser tests (WTH-10, WTH-12): the list counts wait for the
  render that follows `onResize`, so they no longer race the default cell
  size and see 3 hours instead of 6.
- CORE-268 (core spec 0.20): when the group `mm3-kiosk` is missing or the
  change of group on `kiosk.key` fails, the engine now logs the warning
  `kiosk_key_group` naming the group instead of staying silent, and keeps
  running. Citations in the kiosk files reviewed against 0.20 (spec-change
  issue of milestone 3).

### Added

- Spec sync to core spec 0.27: `POST /api/v1/config/patch` also returns
  `documents`, the data of every YAML file of the draft, for the Form view
  (CORE-301).
- Spec sync to core spec 0.26 (editor views for milestone 6 part D): YAML
  view lint through `POST /config/validate`, in-house completion from the
  schema and CodeMirror 6 bundled once with esbuild (CORE-323);
  `GET /api/v1/config/history/{version}` for Open in editor (CORE-324);
  `PUT /api/v1/session/locale` takes `{locale}` (a shipped language or null),
  returns `{locale, effective}` and works only with an admin session
  (CORE-309).
- Spec sync to core spec 0.25 (editor and history API for milestone 6
  part C): `CORE_API_NOTHING_TO_DO` 409 (CORE-298); tagged nodes in JSON Patch
  values as one-key objects and an in-house JSON Patch on the ruamel nodes
  (CORE-320); history routes and answers (CORE-321); `/admin/config` as one
  page with Layout, Form, YAML and History tabs (CORE-322). All belong to
  milestone 6.
- Spec sync to core spec 0.24 (diagnostics details for milestone 6 part B):
  log filters with a minimum level and case-insensitive text (CORE-316);
  `status` frames on change, at most once a second, ignoring `uptime_s`
  (CORE-317); fixed file list of the diagnostic bundle (CORE-318); secret
  values masked as `!masked` in captured bus payloads (CORE-319). All four
  belong to milestone 6.
- Spec sync to core spec 0.23 (REST base gaps, start of milestone 6
  development): `CORE_API_REQUEST_TIMEOUT` 504 (CORE-298 changed); log-level
  API with body `{level}`, null for the default, `GET /api/v1/debug/log-level`
  and the fixed `debug_ttl` expiry (CORE-112 changed); `POST /api/v1/request`
  returns `{payload}`, error replies as 200 and 504 on timeout (CORE-314
  added); `GET /api/v1/modules` entries, idempotent instance controls and 404
  for unknown instances and pages (CORE-315 added). Both new ids belong to
  milestone 6.
- Spec sync to core spec 0.22 (milestone 6 settled): API errors as
  `application/problem+json` with `CORE_API_*` codes (CORE-298), paging of
  history and logs (CORE-299), config ETag with 428 and 409 conflicts
  (CORE-300), editing protocol with `POST /api/v1/config/patch` (CORE-301),
  validate with diff preview and save responses (CORE-302), `!masked` secrets
  (CORE-303), the admin channel `/admin/ws` (CORE-304), `GET /api/v1/status`
  contents with the kiosk object (CORE-305), kiosk dropdowns and run-time volume
  and mute (CORE-306), Show on mirror on the kiosk only (CORE-307), notify
  checks (CORE-308), admin language per session (CORE-309), form label and help
  keys (CORE-310), the local admin socket `/run/mm3/admin.sock` and
  `mm3 diag bundle` (CORE-311), `mm3 diag check` (CORE-312) and OpenAPI as JSON
  only (CORE-313), all in milestone 6. Changed: CORE-152 (section undo uses the
  history sections).

- Packaging after the first manual install, part 1 (core spec 0.20):
  - Service notification (CORE-290): the engine tells systemd its state with
    `sd_notify` datagrams to `$NOTIFY_SOCKET`, on the standard library only and
    a no-op without it: `READY=1` with a `STATUS=` summary once the listeners
    are up, `RELOADING=1` with `MONOTONIC_USEC=` and then `READY=1` around every
    reload (applied or rejected), `STOPPING=1` when shutdown begins. A start
    that aborts never sends `READY=1`.
  - Loop watchdog (CORE-291): with `$WATCHDOG_USEC` set, a task on the engine
    loop sends `WATCHDOG=1` every half interval on the engine clock; a module
    that blocks the loop stops the ping.
  - `mm3.service` (CORE-292) ships in the package (`mm3/core/systemd/`) and
    `mm3 service unit` prints it: `Type=notify`, `WatchdogSec=30s`, the CORE-65
    hardening, state and runtime directories.

- Packaging after the first manual install, part 2 (core spec 0.20):
  `mm3-modules-default` (CORE-294), a distribution without code in
  `modules/mm3-modules-default/` that depends on the published default modules
  `mm3-clock` and `mm3-weather`, each bounded to its current major, and on no
  version of `mm3-core`. CI and the release workflow now build `mm3-clock`,
  `mm3-weather` and `mm3-modules-default` with the engine, and the release
  publishes them with attestations (unchanged module versions are skipped).

- Packaging and deployment, part 3: `install/mm3-install.sh` installs or updates MM3 on Debian 12 and 13 (CORE-293, CORE-268, CORE-143): users and groups, a timestamped venv with a rollback symlink, config and env created only when absent, configuration validated before the switch, units from the package. `install/ci-check.sh` and the CI job `install` run it on clean Debian 12 and 13 images.

- Spec sync to core spec 0.21 (kiosk audio base): PipeWire in the kiosk
  user's session through seat ACLs (CORE-295), the `audio` block with sink,
  volume and mute applied live by the kiosk agent with `pactl` (CORE-296) and
  hardware video decoding (CORE-297), all in milestone 3. Changed: CORE-63
  (`media-src 'self'`), CORE-143, CORE-270, CORE-277 (`sinks` and `audio`
  frames, `failure` area) for milestone 3; CORE-201, CORE-216, CORE-155
  (`audio` top-level key and history section) for milestone 2; CORE-293
  (audio and VA-API packages). Milestones renumbered: 8 Media (new), 9
  Extensions, 10 Release 1.0 (CORE-65, CORE-293, CORE-294).
- Spec sync to core spec 0.20: sd_notify and loop watchdog (CORE-290, CORE-291), `mm3.service` unit (CORE-292), `mm3-install.sh` (CORE-293) and `mm3-modules-default` (CORE-294); CORE-268 now requires `mm3` to be a member of group `mm3-kiosk`. Milestones 6 and 9 updated.

- Spec sync for core spec 0.19 and Module Contract 1.0 rev 12 (milestone 5
  settled): HTTP transport rules behind `ctx.http` (CORE-282..287),
  `engine.location` / `ctx.location` (CORE-288; CORE-201, CORE-211 and
  CORE-246 changed), `WEATHER_CURRENT` in the notification registry, and the
  reference modules greeting, clock and weather (CORE-289, CLK-01..08,
  WTH-01..12). `tools/` learn the `CLK` and `WTH` id prefixes; milestone 5
  now lists its requirements in `milestones.toml`.
- Milestone 5 part A: the engine's HTTP transport and `engine.location`.
  `ctx.http` now sends through one pooled `httpx.AsyncClient` created at
  engine start and closed at shutdown (CORE-282): HTTP/1.1 only, no cookies,
  cache or retries, proxy from `HTTP(S)_PROXY`/`NO_PROXY`, at most 4 requests
  per instance in flight, bodies above 10 MiB refused while read, 10 s default
  and 60 s maximum timeout. Requests carry
  `User-Agent: mm3/<core> <module>/<version>` unless the module sets one, and
  `Host`, `Content-Length`, `Connection` and `Transfer-Encoding` are dropped
  (CORE-283). TLS 1.2+ with verification always on; a grant entry
  `{prefix, ca}` replaces the CA store for its prefix, and a missing file is a
  `CORE_CFG_FILE_UNREADABLE` error at the grant (CORE-284). Loopback,
  link-local, unspecified and multicast addresses are refused with
  `PermissionError`, checked on the address actually connected to and lifted
  only by a grant naming `localhost` or the address literally (CORE-285).
  At most 5 redirects, only to granted URLs, never `https` to `http`, with
  credentials dropped across origins (CORE-286). Errors map to `mm3.errors`
  without library text; `http_request` is logged without query, headers or
  body, and `ctx.health` shows `http_requests`, `http_failures` and
  `http_last_success` (CORE-287). `engine.location` (`lat`, `lon`, optional
  localized `name`), `ctx.location` and `mm3.context.Location` (CORE-288);
  a change restarts instances with a backend (CORE-211), and
  `ModuleHarness(location=...)` sets it in tests. `httpcore` is now a direct
  dependency. The shell side (`welcome.location`, `this.location`) follows
  with part B.
- Milestone 5 part B: the shell side of `engine.location` and the reference
  module layout. `welcome.location` carries `{lat, lon, name}` (the name as
  written) and a change of `engine.location` resyncs the shells (CORE-246,
  CORE-275, CORE-288); `this.location` gives an element the same shape with
  the name resolved along its locale chain, and
  `BrowserHarness(location=...)` sets it in tests. `modules/` holds the
  reference modules (CORE-289): `mm3-greeting`, the code of Module Contract
  section 13 with catalogs in five languages, its own tests and a screenshot;
  CI has a job per module that runs `mm3 module check` and the module's tests.
  `tools/trace_report.py` and `tools/check_spec_refs.py` read `modules/*/src`
  and `modules/*/tests`. Tests name MOD-23 (no outbound HTTP library import).
- Milestone 5 part E: the `mm3-weather` element (WTH-06, WTH-10 and the browser
  part of WTH-12), which closes milestone 5. Icons are Material Design Icons
  (Apache-2.0) vendored as SVG with their licence in `static/icons/`, one per
  condition and a night variant for `clear` and `partly_cloudy`; `icons.js`
  holds the same path data and a test keeps them in step. The three views
  (`current`, `daily`, `hourly`) size their text to the cell and drop rows,
  hours or optional fields instead of overflowing; stale data is dimmed with
  its age (WTH-08) and a missing `http` grant shows a message naming the grant
  (WTH-09). Browser tests cover the three views in five languages and the kit's
  own checks. `mm3-weather` joins the `modules` CI matrix, the coverage report
  is regenerated and milestone 5 is marked done at core 0.19 / contract rev 12.
  `check_spec_refs.py` and `trace_report.py` now also read the `// Implements`
  comments of the reference modules' front-end files (CORE-74, CORE-77, CORE-78).
- Milestone 5 part D: the backend of `mm3-weather` (WTH-01..05, 07..09, 11
  and the backend part of 12). One request per refresh covers current, daily
  and hourly data; the built-in Open-Meteo provider maps WMO codes to the
  closed condition set (unmapped codes become `cloudy`, logged at debug) and
  providers plug in through the entry point group `mm3_weather.providers`.
  The instance publishes only the data of its `type`, stores the last good
  data under `last` (dropped after 24 h, shown as stale after a restart),
  turns network errors, 5xx and 429 into `TransientError`, other 4xx into a
  `ConfigurationError` naming `location` or `provider`, and shows a grant
  message instead of crashing without the `http` grant. `WEATHER_CURRENT` is
  registered in `mm3.notifications` (CORE-224, CORE-225) and sent with UTC
  times (MOD-42). The test kit's MOD-06 check no longer counts temporary
  directory cleanup or the test's own harness as writes outside storage, so
  modules that use `ctx.storage.kv_set` pass `check_contract()`. The element
  follows in part E.
- Milestone 5 part C: `mm3-clock` (CLK-01..08), a front-end-only module in
  `modules/mm3-clock`. Digital and analog display (SVG face with ticks,
  numbers or none), 12/24-hour or locale format, date styles, ISO 8601 week
  number, a time zone per clock and up to four extra zones with a "+1 d"
  marker, always in the mirror's time zone through `fmtDate` (CLK-01..04).
  Updates are scheduled at the next second or minute boundary with
  `this.setTimeout`, so nothing runs between minute boundaries when seconds
  are off (CLK-05). Sunrise, sunset, day length, moon phase, illumination,
  moonrise and moonset are computed in the browser by `astro.js` (NOAA solar
  equations, Meeus low-precision lunar series, no library), with localized
  polar day and night text and a hint when no location is set (CLK-06). The
  tests run `astro.js` in Node against a 28-row table (7 places, 4 dates,
  one above 60 degrees, one in the southern hemisphere) generated with PyEphem:
  sun within 2 minutes, moon within 10, illumination within 1 point (CLK-07).
  Catalogs in five languages with the eight moon phase names (CLK-08). CI
  checks it in the `modules` job.
- Milestone 4 part D: the browser harness and `mm3 new-module` (closes
  milestone 4). `mm3.testing.BrowserHarness` loads a module's element into a
  stand-alone shell page in Chromium (Playwright; `MM3_CHROMIUM` selects the
  executable), served without a socket, under the engine's CSP and with the
  real `mm3.js`; it renders states, resizes the cell, toggles preview and
  locale, takes screenshots, and records CSP violations, network attempts,
  errors and overflow. `run_browser_checks` runs MOD-32 (CSP), 34, 36, 51, 54
  and 84. `mm3 module check` gains the `harness` stage (`--only harness`;
  default is static, kit, harness; exit codes unchanged, a browser that cannot
  start is exit 2). `mm3 new-module <name> --kind full|backend|frontend`
  (CORE-281, MOD-95) writes a src-layout package with the `mm3-build` build
  requirement, all five locale catalogs, a README with the Notifications and
  Grants sections, a sample test and, for front-end kinds, an element that
  passes the check; the front-end-only kind has no hooks. Milestone 4 is done
  in `docs/spec/milestones.toml` (core 0.18, contract rev 11).
- Milestone 4 part C: `mm3 module check <path>` (CORE-281, Module Contract 12.1).
  It builds the module's wheel with `uv build`, installs it in a temporary venv
  with the running `mm3-core[testing]`, and runs the static checks and the test
  kit; `--only static|kit` limits the run, `--format json` and `--junit <file>`
  write machine-readable results, and `.mm3/check-report.json` is always
  written. Exit code 0 means compliant, 1 a MUST failed (a wheel that does not
  build fails MOD-05), 2 the tool could not run (no `uv`, missing testing extra,
  engine not installable). Static checks read the source and the built wheel
  without importing the module: Python by AST and the JavaScript under
  `static/` by an in-house tokenizer for MOD-01–05, 07, 08, 11, 12, 17, 23, 24,
  28–33, 35, 37, 40, 41, 44–47, 52, 60–63, 67, 70, 71, 80 and 86; the 17 kit rows
  run in the venv; the review list names the rows a person confirms.
  `# mm3: allow MOD-xx <reason>` (`//` in JavaScript) silences a SHOULD
  finding and is listed in the report, with no effect on a MUST. The kit
  builds its configuration from the module's `config_model` and opens the
  grants its capabilities need; `[tool.mm3.check] config = {...}, grants = {...}`
  in `pyproject.toml` overrides both. `packaging` is now a dependency.
- Milestone 4 part B: the `mm3.testing` kit (Contract 12.2, core spec 0.18,
  Module Contract 1.0 rev 9). `ModuleHarness` runs one instance on a real
  engine with fakes: `h.http` (mocks, unmocked calls reported) and `h.mqtt`
  (records publishes, `inject()` delivers through the instance inbox), with
  grants checked as on a mirror. It also records notifications, client
  messages, state history and logs, and provides the inputs `notify`,
  `client_message`, `request`, `respond`, `set_locale`, `set_grants`,
  `set_page`, `suspend`/`resume`, `reconfigure` and `restart`. Time is
  virtual on the test's own event loop: `asyncio.sleep`, timeouts, the
  scheduler, budgets and `ctx.now()` move only with `h.advance()`, while
  thread-pool work runs for real (CORE-278). `await h.check_contract()` runs
  every test-kit row of Contract section 11 (MOD-06, 10, 11, 13–16, 24–27, 29,
  42, 43, 70, 72, 83) on fresh instances whose secrets are canaries, and
  raises `ContractError` with the failed ids.
- Engine side of the kit: `ctx.http` (`mm3.http`, HTTP errors in
  `mm3.errors`) and `ctx.mqtt` (`mm3.mqtt`) check the declared capabilities and
  the instance grants, which now reach the engine from the `grants` block. MQTT
  messages are handled through the instance inbox under the handler budget
  (CORE-100). The real transports arrive in milestones 5 and 7. `ctx.health.set()`
  adds fields to the health record (MOD-85).
- Milestone 4 part A: engine prerequisites of the test kit. One engine clock:
  the scheduler, the hook, handler and job budgets, request timeouts and the
  new `ctx.now()` (aware, in `ctx.timezone`) all read it, so a virtual clock
  moves them together (CORE-278, MOD-28). `MirrorModule.emits`, `consumes`
  and `answers` declare notifications and requests with their Pydantic
  models, checked by `mm3.notifications.declaration_problems()` (names,
  model types, registered names use the registry model, no engine-only
  names emitted or answered; MOD-29, MOD-24). New distribution `mm3-build`
  in this repository (a uv workspace member, versioned with the Module API):
  its hatchling build hook `mm3-i18n` compiles every catalog into the wheel
  and fails the build on a catalog error; `mm3-core` builds with it and
  `mm3 i18n compile`/`check` use its compiler (CORE-280). New extra
  `mm3-core[testing]` (pytest, pytest-asyncio, Playwright); `import
  mm3.testing` without it raises `MissingExtraError` naming the install
  command (CORE-279).

- Milestone 3 part E: the kiosk. `mm3 kiosk session` (run by
  `mm3-kiosk.service` inside cage) refuses to start unless `cage -v` reports
  0.1.5 or later, reads the agent key the engine writes to
  `/run/mm3/kiosk.key` (mode 0640, group `mm3-kiosk`) and connects to
  `/ws/kiosk-agent` (loopback, `Authorization: Bearer`) (CORE-143, CORE-268).
  It applies the `display` block with `wlr-randr`, picking the closest
  supported mode, and reports outputs, modes, the applied mode, viewport and
  DPR, also after hot-plugs (CORE-144, CORE-145, CORE-277). It starts
  Chromium once the engine answers, with the CORE-270 flags and a one-time
  kiosk key in `/#kiosk=`, restarts it with a 1-30 s backoff and gives up
  after 5 restarts in 10 minutes (CORE-269). The engine sends the display
  block on connect and on every change, a new kiosk key before each launch
  and `restart` when no kiosk shell has connected for 60 s; it keeps the
  kiosk status (mismatch, Chromium state, restarts, failed). An applied mode
  re-validates the running configuration, resyncs the shells and emits
  `CORE_DISPLAY_CHANGED` (CORE-146). The shell claims the `kiosk` role with
  the key and falls back to a view-only browser when the key is stale.
  `mm3 kiosk session --print-command`, `mm3 kiosk unit`. CI installs
  Chromium for the browser tests, including a smoke test that renders a
  module in five languages and follows the grid on resize (CORE-06).
  Milestone 3 is done.
- New dependency `websockets` (kiosk agent client only, core spec 1.2).
- Milestone 3 part D: internationalization. The engine catalog
  (`mm3/core/locales`, EN, IT, FR, ES, DE) holds every `CORE_*` text: the
  validation messages (now read from it), the engine pages, the WebSocket
  error keys, `REQUEST_TIMEOUT` and the shell's "unavailable" placeholder
  (CORE-11, CORE-40, CORE-117, CORE-263). `mm3 i18n compile` writes
  `messages.mo` and `messages.json` next to every `.po` (plural indices
  converted to CLDR categories); `mm3 i18n check`, now a CI step, fails on
  missing or out-of-date compiled files, missing locales, missing keys and
  different placeholders (CORE-41, CORE-42, CORE-263). The i18n service
  looks keys up along the instance's locale chain in the module catalog,
  locale packages (`mm3.locales`) and the core catalog (CORE-07, CORE-264);
  `/i18n/<instance>.json?v=` serves the merged catalog, whose version
  `welcome` carries with each instance's `locale` and `touch` flag
  (CORE-246). `ctx.t()` with plurals and placeholders, `ctx.format_date()`
  and `ctx.format_number()` (Babel), and in the browser `this.t()`,
  `fmtDate()` (engine time zone) and `fmtNumber()` (CORE-265, CORE-266).
  `HEADER` comes from the module catalog (MOD-67). A locale change sends
  `control locale` and the shell fetches the new catalog and calls
  `onLocaleChange` (CORE-267). Swipes that start on an element with
  `input.touch` stay with it (CORE-235).
- Milestone 3 part C: the shell. The mirror serves the shell at `/` and the
  admin listener the same shell in preview mode at `/admin/preview` (admin
  session required), with no inline script or style (CORE-63, CORE-252).
  Files: `/core/…` (shell, theme, `mm3.js`), `/core/vendor/…` (lit-html
  3.3.2 and Roboto Condensed 5.3.0, vendored with licences and SHA-256 in
  `vendor.json`), `/modules/<module>/<version>/…` (immutable),
  `/appearance/<instance>.css?v=` and `/custom.css?v=` (immutable when the
  version matches) (CORE-252, CORE-262). `/core/mm3.js` exports `html`,
  `svg`, `nothing` and `MirrorElement` (Contract section 5). The shell
  places instances on the CSS grid of the `layout` object, splits preset
  stacks among visible instances, draws headers outside the shadow root,
  adopts module styles, appearance variables and `appearance.css` as
  constructable stylesheets, reports sizes once per frame, applies
  `overflow: fit|scroll`, fades hide/show and page changes, shows the
  failure placeholder, reconnects with 0.5–10 s back-off and jitter and
  reloads on a new `engine_build` (CORE-117, CORE-250, CORE-255..259).
  Theme variables on `:root` (CORE-261). Swipes (25 % of the width, twice
  the vertical travel, within 1 s) and taps are sent as `touch` (CORE-260,
  CORE-276).
- Pages and visibility: a page service keeps the current page, reacts to
  `PAGE_NEXT`, `PAGE_PREV`, `PAGE_SET`, rotation (per page or global,
  paused by interaction for `pause_on_interaction`), touch and
  `CORE_RESUME` (`reset_on_resume`), emits `CORE_PAGE_CHANGED`, and keeps
  the hide reasons `page`, `manual`, `self` and `module:<id>`; hidden
  instances are suspended and resumed when visible again (CORE-233..239,
  CORE-241). Control frames `page`, `show`, `hide`, `status`, `styles`,
  `reload` (`resync` after a hot reload that changes what shells show)
  (CORE-275); `visibility` and `touch` frames from interactive clients
  (CORE-276). `welcome` now carries `timezone`, `touch`, `custom_css` and per
  instance `scripts`, `styles`, `appearance` and `status`; the `layout`
  object carries `grid` (CORE-246, CORE-274).
- Milestone 3 part B: the shell WebSocket. `/ws` on the mirror listener
  (roles `kiosk`, `browser`) and `/admin/preview/ws` on the admin listener
  (role `preview`, admin session and matching `Origin`) (CORE-273).
  Handshake `hello` → `welcome` + one `state` per instance, with the public
  config resolved for each element's locale, the `layout` object, pages and
  `engine_build`; a bad first frame or no `hello` within 10 s closes 4400
  (CORE-246, CORE-273, CORE-274). Roles: `kiosk` only from loopback with the
  current kiosk key, `preview` only on the admin endpoint, any other client
  `browser`, interactive with a display session; refused roles close 4403
  (CORE-247). Frames per CORE-271: `state` with `delta`, `client` both ways,
  `notify` both ways (once per browser with its matching instances),
  `request`/`reply`, `log`. Inbound limits and `error` frames with
  `CORE_WS_*` keys and the echoed `ref` (CORE-248, CORE-272); a frame over
  512 KiB closes 1009. Outbound queue of 1000 frames per client, coalesced
  `state` frames, 1013 when full (CORE-249). Ping every 15 s, drop after
  30 s (CORE-251). Requests from elements reply with `{rid, payload}` or an
  error, `timeout` after the request's timeout (CORE-223, CORE-272). Preview
  clients get no `client` frames (CORE-99). A valid display session cookie
  is renewed on every connection (CORE-254). Backends now reach their
  elements: `mm3 run` connects the engine to the hub.
- Milestone 3 part A: web gateway with the mirror and admin listeners
  (`mm3 run` starts both, `127.0.0.1:8080` and `:8081` by default, CORE-60).
  Both accept loopback plus `server.allow` / `admin.allow`; other sources
  get 403 (WebSockets are closed) and are logged at most once a minute per
  address; `server.allow` applies live (CORE-61). Token store
  `<state>/tokens.json` with named argon2-hashed admin and display tokens;
  `mm3 token new <name> [--display]`, `mm3 token list`, `mm3 token revoke`;
  `/api/*` and `/admin` need an admin token (Bearer) or session (CORE-62).
  Security headers and `Cache-Control: no-store` on every response, no inline
  script or style (CORE-63). Admin sessions via `/admin/login` (HttpOnly,
  SameSite=Strict cookie, 30 days from last use, matching `Origin` for unsafe
  requests, five failures lock an address for 15 minutes) and `/admin/logout`
  (CORE-253). Display pairing: `mm3 token new --display` prints
  `http://<host>:<port>/pair#<token>`; `/pair` sets a display session cookie
  valid until the token is revoked (CORE-254).

#### Module API

- Implemented from Contract 1.0: `ctx.page`, `ctx.pages`, `ctx.layout`
  (with `layout.control`), and the front-end `MirrorElement` with
  `/core/mm3.js` (`html`, `svg`, `nothing`). `t()`, `fmtDate()` and
  `fmtNumber()` use the element's locale; catalogs arrive with the i18n part.

### Changed

- Fixed the intermittent `mm3 module check` MOD-62 failure ("does not match its
  .po"): a `.po` without a `POT-Creation-Date` header made Babel stamp the
  current minute into the compiled `.mo`, so a build and its check a minute
  apart differed. `mm3_build.catalogs` now compiles such files with a fixed
  date (CORE-263, CORE-265).
- Hot reload calls the layout listeners for every applied change, so shells
  resync when instances are added, removed or reconfigured (CORE-275).
- The `Permissions-Policy` header no longer lists `bluetooth`, which
  Chromium does not recognise (CORE-63).
- `tools/check_spec_refs.py` and `tools/trace_report.py` also read the
  `// Implements` and `/* Implements` comments of the engine's front-end
  files (`static/*.js`, `static/*.css`); vendored libraries are skipped
  (CORE-74, CORE-77, CORE-78).
- Browser tests (marker `browser`) run the shell in headless Chromium and
  are skipped where Chromium cannot start; `MM3_CHROMIUM` selects the
  executable.

- Spec sync: Core spec 0.17 and Module Contract 1.0 rev 8 (milestone 4
  requirements). New: one engine clock for the scheduler, budgets and
  `ctx.now()`, virtual in `mm3.testing` (CORE-278); the `mm3-core[testing]`
  extra (CORE-279); the separate `mm3-build` distribution with the
  `mm3-i18n` hatchling hook (CORE-280); `mm3 module check` and
  `mm3 new-module --kind` (CORE-281); wall-clock time only through
  `ctx.now()` (MOD-28); `emits`, `consumes` and `answers` declarations with
  Pydantic payload models (MOD-29). Changed: MOD-24 and MOD-41 (README lists
  the declared names). Module Contract §12 now specifies the harness API,
  static checks, suppressions, harness sizes and the report format.
  Milestone 4 requirements extended in `milestones.toml`.

  **Module API:** `ctx.now()` and the class attributes `emits`, `consumes`
  and `answers` are added to API 1.0 before its first release.
- Spec sync: Core spec 0.16 (kiosk details). Changed: CORE-268 (agent key as
  `Authorization: Bearer`, 4403 otherwise), CORE-277 (`outputs[].current`,
  the agent picks the mode), CORE-146 (the applied mode re-validates the
  running configuration, resync, `CORE_DISPLAY_CHANGED`), CORE-143
  (`wlr-randr --json`, unit file shipped with `mm3 kiosk unit`); `websockets`
  approved for the kiosk agent (1.2).
- Spec sync: Core spec 0.15 (i18n details). Changed: CORE-264 (merged
  catalog at `/i18n/<instance>.json` and its version; locale packages per
  module or `mm3`), CORE-263 (`mm3 i18n check` fails on compiled files that
  do not match their `.po`; backends read `messages.json`), CORE-266 (date
  styles, backend formatting), CORE-246 (per-instance `locale` and `touch`), CORE-235
  (element-owned swipes).
- Spec sync: Core spec 0.14 (shell and kiosk details). New: control
  action contents and `reload` `resync` on hot reload (CORE-275), shell
  input frames `visibility` and `touch` (CORE-276), kiosk agent protocol
  (CORE-277). Changed: `welcome` instance entries carry `scripts`, `styles`,
  `appearance` and `status`, and `welcome.custom_css` (CORE-246);
  `/appearance/<instance>.css` (CORE-252); `visibility`, `touch` and the
  `status` action (CORE-271); outputs reported by the kiosk agent
  (CORE-144); `welcome.timezone` and `welcome.touch` (CORE-246) and the grid size, gap and
  padding in the `layout` object (CORE-274). All in milestone 3. Shell font
  licence corrected to SIL OFL 1.1 (core spec 1.2).
- Spec sync: Core spec 0.13. New: WebSocket frame contents and `ref`
  (CORE-271), error and reply codes (CORE-272), `/ws` and
  `/admin/preview/ws` endpoints, 10 s hello timeout and `engine_build`
  (CORE-273), the `layout` object (CORE-274); all assigned to milestone 3.
- Milestone 2 rework for core spec 0.11 (issue #19). custom.css next to the
  main file is part of the config set: stored in the history (CORE-153),
  compared as the section `custom_css` (CORE-216), restorable alone with
  `mm3 config restore --section custom_css` (CORE-155) and watched for hot
  reload even before it exists (CORE-208). It is validated with the limits
  of CORE-214 up to 64 KiB; a breach is `CORE_CFG_CSS` at `/custom_css`
  (CORE-243, CORE-262). Widget templates are now listed as changed sections
  too. `engine.server.allow` added and applied live as `server.allow`;
  `engine.admin.token` removed: a config that sets it fails with
  `CORE_CFG_UNKNOWN_KEY` (CORE-201, CORE-62).
- Spec sync: Core spec 0.12. CORE-143 changed: `mm3 kiosk session` checks the
  version printed by `cage -v`, comparing only the leading
  `MAJOR.MINOR.PATCH` (Debian 13 ships `0.1.5+20240127`), not the Debian
  release, and uses no cage option or behaviour added after 0.1.5.
- Spec sync: Core spec 0.11 and Module Contract 1.0 rev 7 (milestone 3
  requirements settled). New: WebSocket handshake, roles, limits, reconnect,
  liveness and URLs (CORE-246..252); admin sessions and display pairing
  (CORE-253, CORE-254); element setup, header, stacks, fit and transitions
  (CORE-255..259); swipe (CORE-260); theme variables and `custom.css`
  (CORE-261, CORE-262); catalogs at run time (CORE-263..267); kiosk agent,
  supervision and Chromium flags (CORE-268..270); MOD-37 (templating only
  through `/core/mm3.js`). Changed: CORE-61..63 (allow lists on both
  listeners, token store, security headers), CORE-143 (kiosk system unit,
  Debian 13+), CORE-201 (`server.allow`, no `admin.token`), CORE-153,
  CORE-155, CORE-208, CORE-216, CORE-243 (`custom.css` as section
  `custom_css`), MOD-33 (theme variables). lit-html 3 and Roboto Condensed
  approved (core spec 1.2). CORE-117 moved to milestone 3; MOD-37 assigned to
  milestone 4. Milestone 2 code citing the changed requirements is listed in
  the coverage matrix for review.

### Added

- Configuration history (milestone 2, part D; Core spec 0.10): every valid
  configuration is a numbered version in `<state>/config-history/` with the
  whole config set (main file, includes, widget templates; never the env
  file) and `meta.json` (CORE-153); undo and redo of saves with the parents
  of CORE-154, section restore into the file that holds the section
  (CORE-155), full validation before anything is written (CORE-156),
  sections including `pages` (CORE-216), one flock and atomic writes, no new
  version when the set's hash is unchanged (CORE-217), retention and masked
  secrets in diffs (CORE-218). `mm3 config history|diff|undo|redo|restore`
  with exit codes 0-3 and `--force` (CORE-219). `mm3 run` records versions
  through the history. Milestone 2 is done (53 of 53 requirements).
- Screen layout, display and pages (milestone 2, part C; Core spec 0.9):
  `mm3.core.config.layout` resolves presets to grid cells (CORE-90, CORE-92),
  checks rectangles against the grid (CORE-91, CORE-94), overlaps per `z`
  with MM²-style stacks on a preset (CORE-93, CORE-212), cell size with the
  16px / 32px defaults (CORE-213), `min_size_px` (CORE-96), appearance CSS
  limits and headers (CORE-214, MOD-67), positions required or forbidden
  (CORE-33), the logical viewport from the `display` block, assumed until a
  kiosk reports one (CORE-140..142, CORE-215), and the `pages` block with
  per-page validation that names the page (CORE-230..232). `mm3
  check-config` states the assumed viewport.
- Hot reload (milestone 2, part B; Core spec 0.9): the main file, included
  files, the env file and `widget-templates/` are watched; a reload starts
  500 ms after the last change or on SIGHUP, one at a time with exactly one
  more for changes during a reload (CORE-208). An invalid configuration is
  never applied: the records are logged, `CORE_CONFIG_ERROR` is emitted and
  `disk_valid` turns false (CORE-209). A valid one gets a version and is
  applied per instance in the order of CORE-210 (stop, reconfigure or
  restart, start, re-layout), then `CORE_CONFIG_CHANGED` with the changed
  sections (CORE-216). `ctx.locale`, `ctx.timezone` and `ctx.units` follow
  the reload, with `CORE_LOCALE_CHANGED` broadcast or sent to one instance
  (CORE-211, CORE-245); `reconfigure()` returning True keeps the instance
  running (MOD-50).
- The registry includes `CORE_CONFIG_CHANGED`, `CORE_CONFIG_ERROR` and
  `CORE_LOCALE_CHANGED`; its test now reads the spec table.
- Configuration loading and validation (milestone 2, part A; Core spec 0.8):
  `mm3.core.config` reads `/etc/mm3/config.yaml` (`--config`) with `!include`
  confined to the config directory, 8 levels deep, no cycles (CORE-31), and
  `!env` from the env file `/etc/mm3/mm3.env` (`--env-file`) or the process
  environment, with defaults (CORE-200). It validates the engine model with
  every key of CORE-201, the instance keys and ids (CORE-202), missing,
  unavailable and disabled modules (CORE-203, CORE-204), durations
  (CORE-205), the `backup` block (CORE-244), and each module's
  `config_model` (CORE-32, MOD-45, MOD-49). Every problem becomes a record
  with file, line, column, JSON Pointer and a `CORE_CFG_*` code (CORE-206,
  CORE-243).
- `mm3 check-config [--config] [--env-file] [--strict] [--json]` (CORE-207),
  and `mm3 run`, which refuses to start with exit code 2 on an invalid
  configuration (CORE-30).
- `LocalizedText` and `LocaleTag` in `mm3.config`, resolved through the
  locale chain and in the public config sent to browsers (CORE-101, MOD-48).
- Static checks of a `config_model` for `mm3 module check` (MOD-45 to MOD-47).
- Repository scaffold: packaging (hatchling), MIT license, ruff, mypy, pytest,
  pre-commit, GitHub Actions CI.
- Package skeleton `mm3` with `mm3.core`, `mm3.testing`, `mm3.cli`.
- `mm3 --version` command.
- Dependency management (core spec 10.9): bounded version ranges, `uv.lock`,
  Renovate configuration with automerge for tested patch/minor updates,
  pip-audit job, weekly canary workflow, deprecation warnings as errors,
  constraints file attached to releases.
- Spec references in code (CORE-74): every source file cites the requirement
  ids, spec version and section it implements; `docs/spec/versions.toml`
  records the implemented versions (Core spec 0.2, Module Contract 1.0 rev 3)
  and `tools/check_spec_refs.py` enforces it in CI.
- Python 3.14 in the CI test matrix; the canary tests Python 3.15.
- Renovate: workflow Python versions are never bumped automatically; CI
  tooling majors get their own label and are merged manually.
- Milestone 1, bus, lifecycle and scheduler (Core spec 0.4, Module Contract
  1.0 rev 4):
  - `Notification` envelope with trace id, `ClientMessage`, payload checks
    (CORE-20, CORE-26, CORE-111).
  - Event bus with per-instance bounded inboxes (drop oldest, rate-limited
    warning, drop counter), subscriptions, targeted delivery, sender
    exclusion and endpoints for browsers and bridges (CORE-21..29).
  - Module registry from `mm3.modules` entry points with API version checks
    (CORE-03, MOD-90).
  - Supervisor: eight-state lifecycle, one task group per instance, budgets
    from `engine.timeouts`, restarts with 1/2/4/8 s backoff and DISABLED on
    the fifth failure in 10 minutes, manual restart, live or restarting
    reconfigure, suspend/resume, `CORE_MODULE_FAILED` (CORE-04, CORE-190..197).
  - `MirrorModule`, `ModuleConfig`, `CronExpression` and the first
    `ModuleContext` members: messaging, state, tasks, `run_blocking`,
    scheduler, storage, logger (Contract §3–4, MOD-10..22).
  - In-house scheduler: interval, cron and one-shot jobs on CLOCK_BOOTTIME,
    DST rules, re-planning after clock jumps and time zone changes, no
    overlap, pause on suspend (CORE-180..189).
  - Per-instance storage directory and JSON key-value store (CORE-198).
  - Structured JSON logging to stdout with trace ids and a 5 000-record ring
    buffer (CORE-110, CORE-111).
  - `mm3.testing.VirtualClock` for tests that do not wait.
  - `Engine` wiring startup and shutdown with `CORE_READY` / `CORE_SHUTDOWN`.
- Coverage gate of 85 % enforced (CORE-72).
- Milestone 1 spec changes, Core spec 0.7 and Module Contract 1.0 rev 6
  (issue #8):
  - Envelope fields `expects_reply` and `reply_to` (CORE-20).
  - `ctx.request()` and `MirrorModule.on_request()`: targeted or broadcast
    requests, first reply wins, replies bypass the inbox and `subscribes`,
    timeouts of 0.1–60 s; error replies with `code` `transient`,
    `configuration` or `internal`; `RequestTimeoutError` and `RemoteError`
    in `mm3.errors` (CORE-220..222).
  - `mm3.notifications`: the shared notification registry with payload
    models and JSON Schemas; payloads of registered names are validated
    when modules or the engine send them (CORE-224..226).
  - `public_config()` removes secret fields before a configuration leaves
    the engine (CORE-13).
  - Health record completed with the time of the last message out
    (CORE-113); `ctx.deprecated()` logs `deprecated_api` once per instance
    (MOD-91).
  - Tests for every milestone 1 requirement (CORE-01, 05, 08, 10, 24, 195,
    197, MOD-18..20, 90, 94).

#### Module API

- Added in 1.0: `ctx.request()`, `MirrorModule.on_request()`,
  `RequestTimeoutError`, `RemoteError`, `mm3.notifications`.

### Changed

- Spec references moved to Core spec 0.4 and Module Contract 1.0 rev 4
  (`docs/spec/versions.toml`).

- Requirements traceability (CORE-75..79): Markdown exports of both specs in
  `docs/spec/`, `tools/spec_sync.py` generating `requirements.toml` with
  `changed_in` per requirement, citations checked per requirement with stale
  citations failing only in changed files, `docs/spec/milestones.toml`,
  coverage matrix (`tools/trace_report.py`) and a Traceability workflow that
  keeps one `spec-change` issue per closed milestone.

### Changed

- `docs/spec/versions.toml` is generated and records the current spec
  versions (Core spec 0.6, Module Contract 1.0 rev 6); code may cite any
  version at or after a requirement's last change.
- Spec sync to Core spec 0.7: error replies carry `code` `transient`,
  `configuration` or `internal` (`REQUEST_FAILED` for bugs), and an
  `on_request` over its budget sends no reply (CORE-221).
- Spec sync to Core spec 0.8: the list of `CORE_CFG_*` validation codes with
  severity, Pydantic mapping and params (CORE-243), and the `backup` block
  keys, defaults and checks (CORE-244); both assigned to milestone 2, which
  is now in progress.
- Spec sync to Core spec 0.9: `CORE_CONFIG_ERROR` is a reserved name
  (CORE-22); `CORE_CONFIG_CHANGED`, `CORE_CONFIG_ERROR` and
  `CORE_LOCALE_CHANGED` are registered engine events, with the delivery of
  `CORE_LOCALE_CHANGED` (CORE-245); `layout.gap` and `layout.padding`
  default to 16px and 32px (CORE-213). Milestone 1 stays done at core 0.9
  after reviewing CORE-22 (no code change).
- Spec sync to Core spec 0.10: `pages` is a configuration history section,
  shown in changed sections and restorable alone (CORE-216, CORE-155).

### Removed

- APScheduler dependency: version 4 is still alpha and has an unfixed
  advisory (PYSEC-2026-282, CVE-2026-31072). Jobs will use the in-house
  scheduler `mm3.core.services.scheduler` (core spec 3.3, CORE-180..186).
