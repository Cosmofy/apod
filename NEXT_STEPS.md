# Next Steps

## Archive and globally cache APOD media

This is intentionally deferred until the core APOD and vector-search service is complete.

- Inventory the standard and HD media URLs for all historical APOD records.
- Measure the real total download size before selecting storage.
- Copy eligible images to Amazon S3 and serve them globally through CloudFront.
- Store only the S3 object key or CloudFront URL in Turso.
- Avoid storing image bytes or Base64 strings in Turso or Redis.
- Skip duplicate standard/HD files by comparing their URLs and content hashes.
- Keep externally hosted videos as links; optionally archive only their thumbnails.
- Review copyright and attribution before permanently copying third-party APOD images.

Estimated scale: approximately 10,920 image APOD records and up to roughly 21,840 standard/HD URLs before deduplication.

## Cache repeated search responses

This is intentionally deferred because the current traffic and dataset size do not justify the extra cache logic.

- Normalize equivalent queries, such as capitalization and repeated whitespace.
- Cache the final hybrid-search response in Redis.
- Expire cached searches when the next Mountain Time APOD becomes available.
- Add this only if production telemetry shows repeated searches or embedding latency becoming significant.

## Recommend similar APODs

This is intentionally deferred until the main hybrid-search endpoint is complete.

- Add an endpoint that accepts an APOD date and returns visually or semantically related historical APODs.
- Reuse the selected APOD's stored embedding, so this does not require another OpenAI request.
- Start with an on-demand vector-index query against the existing embeddings.
- Consider precomputing each APOD's nearest neighbours only if production measurements show that on-demand searches are too slow or too frequent.
