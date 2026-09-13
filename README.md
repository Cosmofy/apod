# pictures

Cosmofy's picture microservice. It serves NASA Astronomy Picture of the Day,
APOD discovery/similarity, and NASA Earth Observatory history, search, and similarity.

## endpoints

- `GET /apod?date=YYYY-MM-DD` — Astronomy Picture of the Day.
- `GET /earth-observatory` — current Earth Observatory Image of the Day.
- `GET /earth-observatory?date=YYYY-MM-DD` — exact stored EO publication.
- `GET /earth-observatory/search?q=...&limit=10` — EO lexical/semantic hybrid search.
- `GET /earth-observatory/similar?date=YYYY-MM-DD&limit=10` — EO stored-vector similarity.
- `GET /vector/search?q=...` — APOD hybrid search.
- `GET /vector/similar?date=YYYY-MM-DD&limit=10` — APOD date similarity.

## earth observatory

EO search returns `{"query":"...","search_mode":"hybrid","results":[...]}`.
It falls back to a single available search channel when a dependency fails.
EO similarity returns `{"date":"YYYY-MM-DD","results":[...]}` and uses stored
vectors without an OpenAI call. Both accept limits from 1 to 50, defaulting to 10.
Similarity excludes the source and returns unique dates in descending relevance.

EO records include publication `date`, `title`, `explanation`, `media_type`, `url`,
`url_fallback`, `credit`, `copyright`, `source`, `article_url`, and nullable
`image_date`, `location_name`, `latitude`, and `longitude`. Search/similarity results
also include `relevance_score` in [0, 1]. Verified archive mappings project S3 onto
`url`; source URLs remain available internally as fallbacks. GraphQL consumers
should expose the requested single `url` field. YouTube embeds remain embeds until
their video binaries are separately supplied.

Bulk tools run on Oracle; presigned S3 URLs avoid installing AWS credentials in
the application. `upload_eo_s3_from_manifest.py` streams through temporary files,
resumes completed dates, and logs progress. `import_earth_observatory_media_manifest.py`
maps successfully uploaded records, and `verify_eo_s3.py` checks public image
signatures. Daily EO ingestion/embedding/archive scheduling is a separate follow-up;
the current latest lookup does not generate new EO embeddings.

### separate EO database and offline index build

Set `EO_DATABASE_URL` and `EO_AUTH_TOKEN` to route EO reads/writes to a separate
Turso database. Without `EO_DATABASE_URL`, EO retains the shared APOD database.
APOD always uses `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`. Readiness checks both
databases when a separate EO database is configured.

For the initial archive, the index was built on Toronto A and uploaded as a
finished libSQL database, avoiding a long hosted index build. The recovery tools
`build_eo_offline.py` and `build_eo_index.cjs` copy the existing records/vectors,
apply the verified media manifest, build the vector index, and check integrity.
They are one-off recovery tools with fixed corpus counts and paths: inspect them
before reuse. The Node builder requires `@libsql/client` installed in
`data/eo-builder-node`; the database file and media stay on the remote VM.

`prepare_eo_database_upload.py` creates a **new, empty upload destination**;
`upload_eo_database.py` streams the finished database to that destination. Do not
use an existing production database as an upload target. Verify corpus counts,
vector dimensions, the ordered vector hash, and a live similarity query before
changing the EO connection settings. Keep the old database and a protected
environment backup for rollback. Credentials and generated data are not committed.

## date-based similarity

`GET /vector/similar?date=YYYY-MM-DD&limit=10`

Uses the selected APOD's existing Turso vector, without calling NASA or OpenAI.
`date` is required and uses the same archive and Mountain Time date validation as
`GET /apod`. `limit` defaults to 10 and accepts integers from 1 through 50.

The response is `{"date":"<source date>","results":[...]}`. Each result contains
all `SourceApod` fields and `relevance_score`, calculated as
`max(0, min(1, 1 - cosine_distance))`. Results are ordered by ascending cosine
distance, exclude the source date, and contain unique dates. Different dates may
still feature the same image. There is no nested `picture` or `match_types` field.
An empty `results` array is a successful response.

Errors retain the existing `{"error":{"code":"...","message":"..."}}` envelope:

- Missing, malformed, pre-archive, or future dates use the existing date errors.
- Missing source APOD: `NOT_FOUND` (404).
- Invalid limit: `INVALID_SIMILARITY_REQUEST` (422).
- Missing source vector or unavailable similarity infrastructure:
  `SIMILARITY_UNAVAILABLE` (503).

Livia can forward `x-request-id` and W3C `traceparent` / `tracestate` headers; the
endpoint retains the existing request-ID response header and tracing middleware.
The existing lookup and hybrid text-search endpoints are unchanged.
