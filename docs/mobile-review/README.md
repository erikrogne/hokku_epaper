# Hokku mobile and upstream review

The phone UI lost checked photos every time status polling rebuilt the gallery.
Long galleries also separated upload, frame and admin controls. This change keeps
selections and focused controls stable, adds sticky section navigation and touch
targets, makes dialogs scroll within the viewport, and gives connection failures
and empty collections a clear next action. The existing paper-and-ink design,
collections, progressive gallery reveal and Iconic Patents are retained.

## Preserved fork baseline

The PR targets the fork's existing `feature/collections` branch. It includes the
already-local Iconic Patents commit `c784c4f` and a separate preservation commit
`53381aa` for previously uncommitted live customizations: frame display/device
names, collection behavior, manual Show Next priority, stale scheduler pointers
and reconciliation after render settings change. The matching workspace reload
regression test is included too. Neither original checkout was modified.

This explains why the full PR is larger than the new UI work. Iconic Patents is
existing local work included for preservation, not a newly added feature in this
review. Workspace-only README notes remain in their original dirty checkout.

## Upstream provenance

Upstream was fetched and inspected at
[`4bfe9e3`](https://github.com/defl/hokku_epaper/commit/4bfe9e37b755b3c5ab159f98ea82142801f2ace1).
It had 136 commits beyond the live fork base. These focused changes were integrated
with their regression tests and upstream authorship:

| Upstream commit | Benefit | Integration note |
| --- | --- | --- |
| [`dab8034`](https://github.com/defl/hokku_epaper/commit/dab80348b4a57c96bad180634d2ef43f5093d5c9) | Phone portraits record displayed dimensions, including EXIF rotation. | Cache DB migration repairs dimensions while retaining render slugs. |
| [`8f9fd11`](https://github.com/defl/hokku_epaper/commit/8f9fd114c23cd2315e9ed9fb82d62323fb8f67bd) | Handles rotated TIFF and trailing PNG metadata correctly. | Resolved an import conflict by retaining both imports; kept the format/orientation matrix. |
| [`4b7a154`](https://github.com/defl/hokku_epaper/commit/4b7a1542ce626b888da14a8f7d79c3e93e357f36) | Face boxes use the same decoded frame as rendering, including HEIC/AVIF. | Follow-up needed after AVIF failures on the Mini; updated the detector tests as upstream intended. |

Deferred changes include the color-calibration/LUT pipeline (requires panel
validation and changes render defaults), label filtering (needs reconciliation
with custom collections), upstream screen renaming (overlaps the preserved
display/device-name behavior), and firmware parity/early-wake scheduling fixes
(require firmware and physical-screen testing). There was no upstream merge or
wholesale replacement of the fork.

## PWA behavior and privacy

The manifest supplies a stable start URL, standalone display and resized versions
of the existing logo. The worker is scoped to `/hokku/` and caches only five public
assets: the generic offline page, mobile CSS/JS and two icons. API responses,
configuration, originals, thumbnails, previews, firmware and non-GET requests
bypass the worker. Online pages fetch the current server assets; there is no
background command queue. Offline navigation shows a generic reconnect page.

An updated worker waits for all existing tabs to close. The UI announces the
update without reloading unsaved forms or interrupting uploads. Reopening activates
it and removes the old public cache.

The live bookmark uses HTTP. On that LAN address the worker cannot run; the UI
explains the limitation and remains usable online. Full worker support needs an
existing trusted HTTPS address; this PR does not configure TLS, expose the service
or change security permissions. See the
[service-worker secure-context requirement](https://developer.mozilla.org/en-US/docs/Web/API/Service_Worker_API/Using_Service_Workers).

## Validation

- Python default suite: 1,862 passed and 2 skipped; six loopback-dependent checks
  were blocked by sandbox socket access. Re-running the integration/smoke group
  with loopback access passed all 18 tests, including those six. Time-intensive
  and serial tests remain excluded by the repository's default configuration.
- Final UI/PWA checks: 17 passed after the last template/focus changes.
- Ruff lint and format checks pass across `python/` and the fixture helper.
- Python wheel `4.0.1.dev26` builds; the packaged template and all seven new PWA
  assets match source, and the example configuration contains no Wi-Fi secrets.
- Pyright retains 55 errors in existing Iconic Patents code/tests. The preserved
  baseline had 56; comparison of file/message/rule found zero new diagnostics.
  The removed diagnostic was a missing None assertion in the preserved reload
  test. See [typecheck summary](typecheck-summary.json).
- 50 browser assertions pass in installed Mac Chromium `149.0.7827.55`: repeated
  selections and progressive reveal; collection empty states; repeated dialog
  opening, focus trapping/restoration and safe Cancel activation; unsaved frame
  and config edits; HTTP errors/retry; concurrent request coalescing; offline
  navigation/recovery; update waiting/activation; cache allowlist; and HTTP LAN
  install guidance. No application writes and no JavaScript page errors occurred.
  See [browser evidence](browser-qa.json).
- No page overflow at 320, 375, 390, 430, 768 and 1440px widths, or at 844×390.
  Desktop comparison uses a separate standard desktop context at 1440×900.

The browser testing uses headless Chromium on the Mac Mini, with viewport/touch
emulation for phone views. It is **not actual iPhone testing**. Safari/WebKit,
physical keyboard behavior, Home Screen installation acceptance, standalone
safe-area behavior, real uploads and physical frame delivery were not verified.
The computer-use browser inventory was empty; the installed WebKit executable did
not match the available Playwright version. No browser/global-tool installation
or security bypass was used.

Read-only live probes returned UI/API HTTP 200, zero conversions, zero failures
and one connected screen. The new navigation was absent from the live UI, as
expected: this work is not deployed. OpenClaw, launch agents, live config/data and
accounts were not changed. Nothing was sent from the app.

## Screenshot evidence

Every screenshot uses synthetic fixture data. The fixture rejects non-GET/HEAD
requests before application route handling, uses temporary empty upload/cache
directories, and never connects to a physical screen. No private content or
secrets appear in these images.

| View | Before | After |
| --- | --- | --- |
| Phone, 390×844 | [Before](before-mobile.png) | [After](after-mobile.png) |
| Desktop, 1440×900 | [Before](before-desktop.png) | [After](after-desktop.png) |

Additional phone views: [gallery controls](after-mobile-gallery.png),
[frame card](after-mobile-frames.png), [scrollable details](after-mobile-dialog.png),
[offline recovery](after-mobile-offline.png), and
[HTTP install guidance](after-mobile-install-http.png).

## Reproduce fixture QA

Use an existing Python environment with the repository dependencies and an
existing Playwright installation. All paths below are local fixtures.

```sh
mkdir -p output/playwright
git show 53381aa:python/hokku/webserver/templates/index.html > output/playwright/before-template.html
printf 'hokku-public-v1' > output/playwright/worker-version.txt
PYTHONPATH=python python tools/ui_fixture_server.py --port 18084 --template output/playwright/before-template.html
# In another terminal:
PYTHONPATH=python python tools/ui_fixture_server.py --port 18085 --worker-version-file output/playwright/worker-version.txt
# In a third terminal:
node tools/ui_browser_qa.cjs
```

The screenshot script uses fixed loopback fixture URLs. Do not point it at a live
server. Stop the two fixture processes when finished.
