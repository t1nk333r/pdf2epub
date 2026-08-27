# OCR Provider Registry and Runtime Reliability

## Summary

Turn the existing OCR protocol into a capability-based provider registry that supports local Ollama models, OpenAI-compatible vision endpoints, and dedicated online OCR services without editing provider lists throughout the application. Add real readiness checks, bounded execution, privacy metadata, and provider-specific adapters.

This plan covers audit finding 7 and establishes the extension point for future local and remote OCR models.

## Current State

- `OcrProvider` is a small and useful protocol with one synchronous `recognize` method.
- Provider construction and status are hardcoded in `pdf2epub/ocr.py`.
- Valid provider names are duplicated in the CLI and web request validation.
- Settings are one dataclass containing fields for every current provider.
- Provider status checks configuration presence, not endpoint reachability, authentication, model availability, Arabic capability, or EPUB pipeline readiness.
- The online adapter supports only the OpenAI-compatible chat-completions vision schema; dedicated OCR APIs require custom endpoint and response handling.
- Requests have a timeout but no retry policy, cancellation, health state, or page-level cache.

## Intended Change

Create a single registry that owns provider identity, configuration schema, capabilities, readiness, factory construction, and user-facing metadata. CLI choices and web options must derive from this registry. Providers remain isolated adapters, while routing and source selection consume a stable result contract.

## Decisions and Constraints

- Keep credentials server-side. Never accept API keys in browser forms or include them in logs/reports.
- Distinguish `configured`, `reachable`, `authenticated`, `model_available`, and `ready`; do not collapse them into one boolean.
- Declare whether a provider is local or remote and whether page images leave the host.
- Dedicated OCR services get dedicated adapters. Do not force incompatible APIs through a chat-completions abstraction.
- Standardize exact-text behavior, but allow provider-specific request tuning such as disabling reasoning, image limits, and structured response formats.
- Use bounded retries only for transient failures; never retry authentication or deterministic request errors.
- Preserve synchronous compatibility initially, but put blocking work behind a cancellable worker boundary.

## Core Interfaces

### Provider Descriptor

Define metadata containing:

- stable provider ID and display label;
- local/remote data-boundary classification;
- configuration field definitions and secret markers;
- capabilities such as vision, structured layout, bounding boxes, confidence, batch pages, and supported languages;
- factory callable;
- readiness-check callable;
- default timeout/retry policy; and
- documentation/privacy links where applicable.

### OCR Request and Result

- Replace positional `recognize(image_png, language)` arguments with a request object containing image bytes, media type, page number, language hints, prompt/version, and cancellation context.
- Extend results with provider/model/version, text, optional blocks/bounding boxes, confidence, warnings, timing, and request ID.
- Never place raw credentials or complete provider error bodies in public errors.

### Registry

- Expose `register`, `get`, `list_descriptors`, `create`, and `check_readiness` operations.
- Detect duplicate IDs at import/startup.
- Make built-in registration explicit and deterministic.
- Derive CLI choices and web provider listings from the same registry.

## File-Level Changes

### `pdf2epub/ocr.py`

- Retain shared request/error utilities and protocols, or split them into an `ocr/` package when adapter count exceeds three.
- Move current Tesseract, Ollama, and OpenAI-compatible implementations behind descriptors.
- Add bounded response-size reading, sanitized exception mapping, retry classification, and elapsed-time reporting.
- Parse environment timeout values safely and report configuration errors without crashing provider listing.

### New provider modules

- Add one module per materially different API family.
- Implement a dedicated remote OCR adapter when selected, including its native endpoint and structured layout response rather than assuming chat completions.
- Keep model names configurable. Defaults must be documented and benchmarked rather than embedded as permanent claims.

### `pdf2epub/web.py`

- Remove the provider-name whitelist and validate against the registry.
- Return provider capabilities and detailed readiness state from `/api/ocr/providers`.
- Show clear Arabic labels for local versus remote processing and require explicit acknowledgement before sending document pages remotely.
- Disable providers that are not ready while preserving diagnostic messages.

### `pdf2epub/cli.py`

- Populate choices/help from the registry.
- Add `pdf2epub providers` or equivalent diagnostics showing readiness and configuration guidance without secrets.

### `pdf2epub/models.py` and reporting

- Record provider ID, model, adapter version, remote/local classification, timing, retries, and confidence for every OCR-selected page.
- Record only sanitized errors.

## Implementation Steps

1. Define request/result, descriptor, readiness, and registry interfaces with unit tests.
2. Register existing Tesseract, Ollama, and OpenAI-compatible adapters without changing external behavior.
3. Derive web and CLI provider validation/options from the registry.
4. Implement layered readiness checks and short-lived caching of their results.
5. Add sanitized errors, response-size bounds, transient retry policy, and cancellation hooks.
6. Add provider/model metadata to page reports.
7. Add the first dedicated remote OCR adapter using its native API contract.
8. Add privacy acknowledgement and local/remote indicators in the UI.
9. Document adapter authoring, configuration, health diagnostics, and data-boundary behavior.
10. Benchmark candidate local and remote models against the same labeled Arabic corpus before changing defaults.

## Tests

- Registry duplicate-ID, lookup, construction, and derived-choice tests.
- Readiness tests for missing configuration, unreachable endpoint, bad authentication, missing local model, healthy model, and malformed environment values.
- Adapter contract tests using recorded sanitized fixtures for each API family.
- Timeout, cancellation, bounded-response, retryable, and nonretryable error tests.
- Tests proving public errors and reports never contain API keys or authorization headers.
- Web tests showing provider options come from the registry and remote providers require acknowledgement.
- End-to-end fake-provider tests through analysis, selection, reporting, and EPUB publication.
- Benchmark harness that reports accuracy, omissions, hallucinations, latency, and memory use per provider/model.

## Acceptance Criteria

- A provider is added in one registration location plus its adapter/tests; CLI and web require no provider-name edits.
- Provider status accurately distinguishes configured from operational readiness.
- A missing Ollama model or unreachable endpoint is visible before conversion starts.
- Dedicated OCR APIs are supported without pretending to be OpenAI chat-completions endpoints.
- Remote processing is clearly disclosed and explicitly acknowledged.
- OCR requests are bounded, cancellable, retried only when safe, and reported without secrets.
- Model defaults are selected from benchmark evidence on representative Arabic pages.

## Risks and Mitigations

- **Over-generalized interface:** keep provider-native data available in an extension field while standardizing only common semantics.
- **Slow readiness endpoints:** use short timeouts and cache health results briefly.
- **Provider API drift:** version adapters and maintain sanitized contract fixtures.
- **Accidental remote data transfer:** require descriptor classification plus UI acknowledgement and test it end to end.

## Dependencies and Ordering

- The registry foundation can be implemented in parallel with quality-signal work.
- Candidate comparison must consume the standardized OCR result contract.
- Benchmark-driven default selection depends on the labeled corpus from the content-integrity plan.
