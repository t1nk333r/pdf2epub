# EPUB Validation and Page-Image Invariant

## Summary

Make EPUB validity a required publication gate and enforce the product rule that rendered or scanned PDF pages must never be packaged as EPUB illustrations. Generate artifacts atomically, validate them before success, and distinguish genuine figures from page-derived raster content using explicit provenance and conservative policy.

This plan covers audit findings 3 and 5.

## Current State

- `pdf2epub/epub.py::build_epub` writes directly to the final output path.
- EPUBCheck runs only when an executable happens to be on `PATH`; missing validators and validation errors become warnings.
- Unit tests call `build_epub(..., validate=False)`, so they verify ZIP strings but not EPUB conformance.
- Embedded-image preservation uses page-coverage thresholds. It rejects obvious full-page and tiled rasters but cannot prove that every retained image is a genuine figure.
- The pipeline marks a job complete even when validation is skipped or reports errors.

## Intended Change

Only publish and expose an EPUB after all of these gates pass:

1. content integrity succeeds;
2. ZIP and XML internal checks succeed;
3. EPUBCheck returns no errors;
4. the packaged-image provenance audit proves no page render or page-scan raster was included; and
5. a basic reader smoke test can open and enumerate the spine.

Failures must leave diagnostics and the quality report but no artifact presented as a valid downloadable EPUB.

## Decisions and Constraints

- EPUBCheck is a required runtime dependency for conversion, not an optional production enhancement.
- Validation warnings may remain nonfatal only when explicitly allowlisted and documented; errors are always fatal.
- Write to a per-job temporary file and use atomic replacement after all gates pass.
- Never mark the first arbitrary internal image as a cover. Cover metadata requires an explicitly selected and validated cover asset.
- Images created by rendering a PDF page are ineligible for EPUB packaging, even when cropped, unless a future explicit review workflow reclassifies a crop as a figure.
- Fail closed when image placement or provenance cannot be determined.

## Data and API Changes

### `pdf2epub/models.py`

- Add image provenance metadata: PDF object reference, page number, placements, pixel dimensions, displayed coverage, extraction method, and classification reason.
- Represent cover selection explicitly rather than deriving it from image order.
- Add validation result models with tool version, errors, warnings, timestamp, and artifact hash.

### `pdf2epub/analyzer.py`

- Return image records instead of untyped `Path` entries.
- Deduplicate repeated xrefs and retain every placement for classification.
- Classify an image as a candidate figure only when all placements and total page coverage are understood.
- Reject known page backgrounds, tiles, masks, page renders, and uncertain placements.
- Record rejected-image reasons in the quality report so the invariant is auditable.
- Keep the current conservative area checks as signals, not the complete policy.

### `pdf2epub/epub.py`

- Accept only approved image records.
- Remove the “first image is cover” behavior. Add `cover-image` only for an explicit cover record and generate the appropriate cover document/metadata.
- Build to a temporary EPUB in the destination filesystem.
- Run internal ZIP checks and parse every XML/XHTML document.
- Invoke EPUBCheck with a bounded timeout and capture structured diagnostics.
- Raise a typed validation exception on errors or missing validator.
- Return validation metadata rather than warning strings.

### `pdf2epub/pipeline.py`

- Coordinate temporary output, integrity checks, EPUB validation, and atomic rename.
- Write the quality report even when EPUB validation fails, including diagnostics.
- Return an EPUB path only after successful publication.

### Packaging and Runtime

- Choose and document one reproducible EPUBCheck installation strategy:
  - pinned Java/JAR fetched during a documented setup step with checksum verification; or
  - pinned system/container package in a deployment image.
- Record the validator version in every quality report.
- Add a startup/preflight endpoint that reports validator readiness.

## Implementation Steps

1. Add typed image provenance and validation-result models.
2. Refactor extraction to deduplicate xrefs and report accepted/rejected image decisions.
3. Define the page-raster invariant as a pure policy function with adversarial tests.
4. Remove implicit cover selection and add explicit cover handling.
5. Split EPUB construction from publication.
6. Add internal archive/XML validation.
7. Make pinned EPUBCheck execution mandatory with a timeout.
8. Add atomic publication and cleanup of failed temporary artifacts.
9. Propagate typed failures through CLI and web job states.
10. Update installation, troubleshooting, and Apple Books documentation.

## Tests

- EPUBCheck integration test against every generated fixture EPUB.
- Negative fixtures for invalid OPF, manifest media types, malformed XHTML, missing resources, bad navigation, and corrupt image bytes.
- Tests proving validation failure never returns or exposes a final EPUB path.
- Tests proving output replacement is atomic and an existing valid file survives a failed rebuild.
- Image-policy tests for full pages, tiles, masks, clipped page scans, repeated xrefs, overlapping placements, uncertain geometry, and genuine figures.
- Package inspection test asserting every image has approved provenance and no render-work filename/path can enter the archive.
- Explicit cover tests for books with no cover, a real cover, and internal figures only.
- Apple Books manual smoke-test checklist for release candidates.

## Acceptance Criteria

- Conversion cannot reach `complete` without a successful pinned EPUBCheck run.
- EPUBCheck errors are fatal and visible in the downloadable report.
- Failed construction or validation cannot leave a partial file at the final download path.
- No image is marked as a cover solely because it is first.
- Every packaged image has approved figure or explicit-cover provenance.
- Adversarial page-raster fixtures are rejected, including cropped and tiled variants.
- The web readiness response distinguishes “application running” from “EPUB publishing ready.”

## Risks and Mitigations

- **EPUBCheck distribution complexity:** pin one supported method and verify checksums/version at startup.
- **False rejection of legitimate large figures:** record decisions and allow later human review, but preserve the default fail-closed invariant.
- **Long validation time:** enforce a timeout and validate only after content gates pass.
- **Platform differences:** test macOS and Linux in CI using the same validator version.

## Dependencies and Ordering

- Integrate with the content-integrity plan's publication gate.
- Land typed image provenance before changing EPUB packaging.
- Complete validation infrastructure before claiming Apple Books compatibility.
