# APOD S3 media handoff for the Livia / Java GraphQL agent

Deployment status (2026-09-12): both Toronto nodes deployed and verified. 10,966 APOD dates have compatible verified S3 keys. 23 uploaded-source date mappings were excluded due to media-type mismatches. The two failed source downloads and other unarchived media retain original URLs. 106 unit tests pass. Anonymous media HEAD succeeds; anonymous manifest/list requests return 403.

## Requested GraphQL change
The APOD microservice now owns media URL selection. Keep consuming its existing endpoints:
- GET /apod?date=YYYY-MM-DD (date optional)
- GET /vector/search?q=black%20hole&limit=10
- GET /vector/similar?date=YYYY-MM-DD&limit=10

Base URL: https://pictures.api.cosmofy.services.deployim.com

The APOD REST contract has exactly two public media fields: `url` and nullable `url_fallback`. `hdurl` is internal NASA-source metadata and is never returned. In GraphQL, expose only `url`; do not expose `hdUrl`, `fallbackUrl`, or a second media field. Apply this to exact APOD, search results, similarity results, and APOD artifacts if they have separate mappings. Do not create a second image lookup call or compute S3 hashes in Java.

## Response behavior
For a verified, media-type-compatible uploaded asset:
- url = permanent HTTPS S3 HD object URL
- url_fallback = original NASA HD URL, or original URL if HD was absent
- date, title, explanation, media_type, credit, copyright remain unchanged
- relevance_score and search match_types remain unchanged
- no nested picture object is introduced
- s3_object_key is internal and is NOT returned by the API

For a failed, unarchived, or media-type-incompatible asset:
- `url` is the original playable source URL
- `url_fallback` is null
- embedded videos retain the original player link unless an actual compatible video file was verified

`url_fallback` is available at the REST boundary for operational/debug consumers, but it is intentionally not part of GraphQL or the client contract. GraphQL and the app use `url` only.

## Storage and safety
Turso production database: apod-v2-us, Ohio.
New nullable column: apods.s3_object_key.
Original media_url and hd_media_url remain untouched. There are no image blobs or base64 strings in Turso.
An object key is set only from the checksum-verified upload manifest and when the date, source URL, and media type match the live record. Repeated images can share one object key.
The API constructs the URL locally from the stored key: no per-request S3 HEAD/GET, no new AWS credentials in the app.
Redis retains the internal key but the response excludes it.
An upsert that changes the source URL or media type clears the old key to avoid serving an unrelated file.

Bucket: cosmofy-apod-hd-010025084205-eu-west-2
Region: eu-west-2 (London)
Base: https://cosmofy-apod-hd-010025084205-eu-west-2.s3.eu-west-2.amazonaws.com
Public access: HTTPS GetObject under hd/ only. No anonymous list/upload/delete.
The manifests/ prefix remains private. No CloudFront or custom domain was added.
URLs are not presigned and have no built-in expiry, but stop working if objects are deleted, permissions change, or the bucket is removed. Public downloads generate bandwidth costs.

## Future daily APODs
This release connects the existing archive; it does NOT add an automatic daily S3 media downloader.
New/unarchived days safely use original NASA URLs until uploaded and verified.
A separate trusted upload job must update s3_object_key only after successful upload and verification, then evict that date's Redis entry if it is cached.
Never set a key merely because a deterministic hash can be generated.

## Cache coordination
Existing Java/GraphQL/Stellate cached responses can still contain NASA URLs until expiration or a targeted APOD invalidation. Invalidate relevant APOD results after the GraphQL deployment if immediate visibility is required. Do not purge unrelated services.
No Livia repository or GraphQL schema was edited by the APOD agent.

## Operator notes
APOD runs on Toronto A and B, loopback port 29401, systemd cosmofy-apod.service.
Migration: migrations/005_add_s3_object_key.sql (additive).
Importer: PYTHONPATH=. uv run python scripts/import_media_manifest.py VERIFIED_MANIFEST.json
Only use the trusted private backfill manifest, never arbitrary user-submitted manifests.
Original NASA media URL fields are checked unchanged by the importer.
Rollback code backups:
- B: /home/ubuntu/apod-media-rollback-8exlmqff
- A: /home/ubuntu/apod-media-rollback-8r54im29
The old code ignores the new column, so it can be restored without dropping data.

## Verification required in GraphQL
1. Fetch date 2019-04-11: `url` must be S3 and `url_fallback` must be NASA.
2. Fetch search and similarity results: same URL mapping, scores/order/date uniqueness preserved.
3. Confirm nullable fallback serialization does not fail older/unarchived entries.
4. Preserve request-ID and W3C trace propagation.
5. Verify the client loads the image and uses fallback only after failure.
6. Keep author/copyright metadata visible; mirroring does not remove attribution or licensing obligations.
