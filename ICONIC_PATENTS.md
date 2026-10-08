# Iconic Patents

This document describes the Iconic Patents extension for Hokku. The extension
keeps patent records and source artwork separate from the final e-ink
framebuffer so the layout can evolve without redownloading or rebuilding the
historical drawings.

## Architecture

Iconic Patents is a small extension of Hokku's existing image and collection
pipeline:

1. The importer reads a CSV or XLSX catalog and upserts patent records into a
   JSON manifest.
2. Verified records are retrieved from an authoritative patent source. The
   original source PDF and a grayscale figure image are stored in the patent
   asset library.
3. A stable `patent-<PATENT_NUMBER>.png` bridge image is placed in Hokku's
   normal upload directory. This makes the record visible to the existing
   image manager, collection store, scheduler, and rotation controls.
4. When a bridge image is selected, Hokku's patent renderer composes the
   original drawing, structured metadata, and a QR code into the same display
   framebuffer format used by ordinary photos.
5. Rendered panel bytes and preview PNGs are cached using the patent record,
   template version, display model/orientation, and image settings. The cache
   is an optimization; the manifest and original artwork remain the source of
   truth.

No second database, collection implementation, scheduler, or image pipeline
is introduced.

## Library layout

With `--config`, the default library is next to Hokku's configured data
directory.  In practice the CLI takes the parent of `AppConfig.cache_dir`, so
the normal `/var/lib/hokku/cache` setting produces
`/var/lib/hokku/iconic-patents/`:

```text
<hokku-data>/iconic-patents/
  manifest.json
  patents.csv
  import-errors.json
  images/
    US821393A/
      original.png
      source.pdf
      metadata.json
  renders/
    <display-model>/
      <orientation>/
        <cache-key>.png
        <cache-key>.bin.zst
```

The patent number is normalized for stable directory and bridge-image names.
Candidate rows without a patent number receive a deterministic candidate ID
based on their source-row content. They are retained in the manifest but are
not downloaded until a number and authoritative source have been verified.

## Record model and status

Each record carries the catalog fields plus retrieval and display state:

- `id`, `simple_name`, `patent_title`, `inventor_names`
- `patent_number`, `patent_year`, `patent_date`
- `description`, `category`, `source_url`, `original_patent_document_url`
- `image_url`, `local_image_path`, `recommended_figure`, `recommended_page`
- `qr_destination_url`, `iconicity_score`, `visual_quality_score`
- `verification_status`, `collection_ids`, and the bridge `image_name`

Statuses are explicit:

```text
candidate -> needs_verification -> verified -> asset_ready -> display_ready
                                                              \-> rejected
```

The importer preserves manual fields and collection membership on subsequent
runs. A failed retrieval is recorded in `import-errors.json` with the patent,
step, error, and retryability.

## Source and verification rules

- Prefer Google Patents, USPTO, EPO, WIPO, or the original patent-office
  document.
- Use the authoritative patent number and date, not a search-result guess.
- Use the original patent drawing. Do not generate, redraw, stylize, or
  reinterpret the art.
- Keep uncertain candidates as `needs_verification`; do not fill missing
  inventors, dates, or numbers with guesses.
- Store the canonical source URL and the original PDF URL when available.
- The normal QR destination is the canonical Google Patents page, for example
  `https://patents.google.com/patent/US821393A/en`.
- Retrieval is restricted to the configured authoritative hosts. The renderer
  refuses to create a QR code when there is no valid destination URL.

The initial supplied workbook contains 1,000 rows. Twelve rows are already
verified and have distinct patent numbers. The remaining 988 rows are
discovery candidates and must remain visibly unverified until researched.

## Importing a catalog

The importer is resumable and idempotent. Patent number is the external
deduplication key when present; the deterministic candidate ID is used before
verification. Existing manual fields, collection membership, and downloaded
assets are not reset by a repeat run.

The normal import flow reads `AppConfig` from `--config`.  That supplies the
Hokku upload directory for stable bridge copies, the render cache directory,
and the default patent-library location.  Explicit directory flags override
those values, which is useful for a staging library or an offline test:

```bash
python tools/iconic_patents.py import /path/to/catalog.xlsx \
  --config /path/to/config.json --no-network

# Limit a run while validating a new catalog or source policy.
python tools/iconic_patents.py import /path/to/catalog.xlsx \
  --config /path/to/config.json --limit 10 --no-network

# Override the library and Hokku upload locations while retaining config-based
# image settings and cache behavior.
python tools/iconic_patents.py import /path/to/catalog.xlsx \
  --config /path/to/config.json \
  --library-dir /tmp/iconic-patents \
  --upload-dir /tmp/hokku-images \
  --no-network

# Export the current structured manifest as a CSV.
python tools/iconic_patents.py export-manifest --config /path/to/config.json
```

`--library-dir` is optional when `--config` is present.  Without a config, the
CLI defaults to the local `data/iconic-patents/` library and does not assume
that Hokku's system upload directory is writable.  `--cache-dir` is an
additional explicit override for render caches.  The precedence is:

1. an explicit `--library-dir`, `--upload-dir`, or `--cache-dir`;
2. the corresponding value derived from `--config`;
3. the safe local defaults (`data/iconic-patents/` and its local render cache).

`--dry-run` does not create a missing config file, manifest, asset, or retry
error file.  Use `--no-network` whenever validating or importing a catalog
without retrieval consent; it stores source rows but performs no patent-data
downloads.  On a config-backed, non-dry import, records with a produced bridge
file in the configured upload directory are also added to the real Hokku collection named exactly
**Iconic Patents** in `collections.json` under the effective
`AppConfig.cache_dir`.  This membership update is idempotent.

If `--config` is omitted, the CLI does not touch Hokku's collection store,
even when `--upload-dir` is supplied.  If the run produces no bridge image,
the CLI likewise leaves `collections.json` alone instead of creating an empty
collection.  Candidate-only and dry-run workflows therefore remain safe for
local catalog review.

The XLSX reader uses the workbook's XML directly, so importing does not depend
on a spreadsheet application. CSV input uses the same column names. A later
100- or 1,000-row catalog can be rerun against the same library; already known
patents are updated without duplicate bridge images.

For a failed run, inspect `import-errors.json`, fix or retry the affected
source, then rerun the same command. Completed records are skipped or reused,
so a failure around row 437 does not require starting over.

## Rendering

The patent template is optimized for Hokku's monochrome e-ink displays:

- the original drawing occupies most of the usable canvas;
- the invention name is the largest metadata line;
- inventor and year are secondary;
- the patent number is tertiary;
- a compact QR code points to the canonical source;
- whitespace and grayscale are preserved; no gradients or decorative image
  treatments are applied.

For the 13.3-inch Spectra 6 panel in portrait (`huessen_epf1301`), the visible
card is 1200×1600 pixels, matching the physical 7.8-inch width by 10.6-inch
height. The portrait composition gives the drawing the left two-thirds of the
card and uses a right-hand rail for the title, inventor/year/patent metadata,
description, and QR caption. Patent API previews and offline exports default to
this portrait profile for `huessen_epf1301`; pass `--orientation landscape` (or
the API equivalent) for an explicit override. An unconfigured live screen uses
the same Huessen portrait default for patent records; an explicit per-screen
orientation remains authoritative, and ordinary photos are unaffected.

Live display rendering and offline export use the same renderer. The cache key
includes the record fingerprint, template version, display model, orientation,
and render settings. Editing metadata or replacing the selected figure
therefore invalidates the affected render without touching unrelated records.

## Collections and administration

Patent images remain ordinary Hokku items for browsing, selection,
inclusion/exclusion, additional collection assignment, and normal
random/sequential/shuffle rotation.  A config-backed import ensures the real
**Iconic Patents** collection exists through Hokku's existing
`CollectionStore`, then adds each imported bridge image filename to it.
`--collection-id` remains an additive override for record metadata and does
not replace or rename the automatic collection; it still does not create an
arbitrary collection.  Further collection management remains available
through Hokku's collection API/UI.

Patent detail exposes the structured fields, source, original art, selected
figure/page, verification and quality scores, plus actions to open the source,
preview or rerender the display image, and download the original PDF or the
display-ready PNG.

## Offline export

`export-png` renders every `display_ready` record through `PatentRenderer` and
writes final display-ready PNGs without any network access.  It uses the same
image settings and cache directory derived from `--config` as the live path:

```text
exports/iconic-patents/
  0001-airplane-US821393A.png
  manifest.json
```

Example:

```bash
python tools/iconic_patents.py export-png \
  --config /path/to/config.json \
  --output-dir exports/iconic-patents \
  --model huessen_epf1301
```

`export` is accepted as a short alias.  Use `--library-dir` to export a
staging library, `--cache-dir` to isolate renderer caches, `--orientation` to
override the model default, and `--limit` to export a small sample. The Huessen
EPF1301 default is portrait; other models default to landscape. The export
manifest records the patent ID, source,
source artwork, verification status, model, orientation, output path, and any
per-record render errors.  A non-zero exit status means at least one selected
record could not be rendered; the manifest is still written so the failure is
recoverable.  It is safe to rerun after changing the template or display
resolution.

## Adding and reviewing more patents

1. Add rows using the catalog schema and leave uncertain fields blank.
2. Run the importer with `--limit` on a small batch.
3. Verify number, inventor, date, source, and the selected figure against the
   original document.
4. Review the grayscale preview at the target display size.
5. Mark suitable records `display_ready`; reject dense, illegible, tiny, or
   materially duplicated artwork.
6. Run the full import/export after the sample passes.

Collection membership is managed through Hokku's existing collection API and
UI. Manual edits are authoritative and survive imports.
