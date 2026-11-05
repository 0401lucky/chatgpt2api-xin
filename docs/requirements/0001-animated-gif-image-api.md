# 0001: Animated GIF image APIs

Status: pending-verification

- Owner: Image protocol and ImageStorageService maintainers
- Created: 2026-11-05
- Updated: 2026-11-05
- Reference: [basketikun/chatgpt2api PR #424](https://github.com/basketikun/chatgpt2api/pull/424), commit `3ef7edd6839db0433507bcfd17d0c8a754f558da`
- Related decisions: [ADR 0003](../adr/0003-deepen-image-storage-before-replacing-the-catalog.md), [ADR 0004](../adr/0004-use-one-application-database-with-domain-repositories.md)

## Problem and user outcome

Callers need text-to-GIF and reference-image-to-GIF entry points. The existing
image pipeline stores every result under a PNG filename, serves remote copies
as PNG, and may flatten animation when upscaling. Its configured request budget
can expire before a GIF finishes generating.

## Scope and Interfaces

- Add `POST /v1/images/generations/gif` and `POST /v1/images/edits/gif` using the
  existing generation and edit inputs, authentication, filtering, Call Records,
  account selection, and error handling. GIF routes return non-streaming JSON.
- Ask upstream for a looping GIF. Apply a request-local 300-second stream budget
  and 300-second result-poll budget, with the existing 30-second lifecycle
  allowance. Ordinary image requests retain their configured budgets.
- Preserve GIF bytes, extension, MIME type, frames, and loop metadata through
  local/WebDAV storage, synchronization, public reads, and downloads. Skip
  static-image upscaling for GIFs and other animated images.
- Add `mime_type` and `animated` to image result items. Keep the existing
  `url` versus `b64_json` response contract and measured dimensions.
- Include GIFs in gallery discovery and disk-threshold cleanup.

## Non-goals

No new Studio controls, model identifiers, animation renderer, automatic
conversion of static images, database migration, release, or server deployment.
Upstream decides the returned format; the API reports the actual result and
does not claim that a static image is animated. The existing Codex image
transport still requests PNG output.

## Unique owners and persistence

The existing Image Asset, Image Attempt, Call Record, and Upstream Account
terms from [CONTEXT.md](../../CONTEXT.md) apply without new domain entities.

| Concern | Owner and consumed Interface |
| --- | --- |
| HTTP variants, authentication, input validation | `api.ai.create_router` and `api.image_inputs` |
| Request lifecycle, retries, deadline, result projection | `ConversationRequest` and the image protocol pipeline |
| Upstream HTTP/SSE consumption | `OpenAIBackendAPI` |
| Asset format detection, files, catalogue, WebDAV mutation | `ImageStorageService` and its existing mutation interfaces |
| Optional static-image resizing | `upscale_image_if_needed` |
| HTTP asset representation | `image_service` |

Image Assets remain in local/WebDAV storage and the existing JSON catalogue.
Existing asset locks, tombstones, retention, and cleanup ownership remain in
effect. Thumbnails may remain static PNG previews; originals retain animation.
No frontend interaction or responsive-layout change is required. No new ADR is
required because ownership and persistence boundaries do not change.

## Security and failure behavior

GIF routes reuse input count/size limits, remote-reference validation, content
filtering, and bearer authentication. Timeouts are internal request options,
not caller-controlled global configuration. Cancellation, account switching,
partial success, and storage errors use the existing image lifecycle. Real
upstream generation remains an external verification step.

## Acceptance and verification matrix

| Criterion | Verification | Status |
| --- | --- | --- |
| Both GIF routes share input/auth/filter behavior and force JSON responses | Isolated FastAPI contract tests | passed |
| GIF stream, transport, polling, and lifecycle budgets reach execution without changing normal requests | Protocol tests with mocked upstream transport | passed |
| GIF frames and bytes survive formatting, storage, reads, sync, and download with upscaling enabled | Generated multi-frame fixtures and temporary local/WebDAV storage | passed |
| PNG behavior, gallery discovery, and GIF cleanup remain correct | Targeted regression tests | passed |
| Supported upstream account returns an actual animated GIF | Live request and user acceptance | pending |

Before edits, `python -m pytest -q` fails during collection because the local
`tests/test_image_model_not_found.py` imports the absent
`IMAGE_MODEL_NOT_FOUND_CONTEXT`. This baseline is separate from GIF verification.
Local regression test sources follow the repository's ignored `tests/` policy.
`python -m pytest -q tests/test_gif_images.py --tb=short` passes all 32 cases.
The existing Sentinel tests report 13 failures, 6 passes, and 4 skips both in
the working tree and in an isolated export of the unchanged `a4e8100` baseline;
their failures are not introduced by this change.

## Documentation and rollback

Update README, README_EN, CHANGELOG, and the current backend map with the new
routes, response metadata, timeout behavior, and upstream limitations. Reverting
the change removes GIF entry points; existing GIF files must remain available
through a version that recognizes GIF assets. No data migration is performed.
