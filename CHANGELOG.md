# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Module API changes are listed
separately under **Module API** in each release (Module Contract, section 10).

## [Unreleased]

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
  before a removal), CORE-121 and MOD-96..99 (module-index).

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
