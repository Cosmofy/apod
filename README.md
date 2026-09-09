# apod

astronomy picture of the day microservice for Cosmofy app

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
