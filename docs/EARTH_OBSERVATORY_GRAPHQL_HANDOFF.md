# Earth Observatory GraphQL handoff

The APOD service now exposes the current NASA Earth Observatory Image of the Day at:

```text
GET https://pictures.api.cosmofy.services.deployim.com/earth-observatory
```

It is a current-item endpoint only. It reads NASA's official EO RSS feed, enriches the feed item from NASA's article and Explorer data, and serves Redis -> Turso -> NASA. It does not perform historical EO import, S3 archival, vector indexing, or scheduled ingestion yet.

## REST response

```json
{
  "date": "2026-09-11",
  "title": "Monterrey Amid Mountains",
  "explanation": "Full cleaned NASA editorial article body...",
  "media_type": "image",
  "url": "https://assets.science.nasa.gov/.../ISS075-E-70481_lrg.jpg",
  "url_fallback": "https://assets.science.nasa.gov/...thumbnail...",
  "credit": "Astronaut photograph ISS075-E-70481 was acquired...",
  "copyright": null,
  "source": "earth_observatory",
  "image_date": "2026-08-26",
  "location_name": null,
  "latitude": 25.6867,
  "longitude": -100.3162,
  "article_url": "https://science.nasa.gov/earth/earth-observatory/monterrey-amid-mountains/"
}
```

`latitude`, `longitude`, `location_name`, `image_date`, `credit`, and `copyright` are nullable because NASA does not provide each field for every EO post. Do not treat missing coordinates as an error.

`url` is the only primary media URL GraphQL should expose. `url_fallback` is optional recovery metadata and should not be exposed in the initial GraphQL schema. The current EO wrapper does not yet archive media to Cosmofy's S3 bucket, so `url` currently points to the NASA high-resolution asset.

## Requested GraphQL shape

Keep APOD under `pictures.apod` and add EO under `pictures.earthObservatory`:

```graphql
type Query {
  pictures: Pictures!
}

type Pictures {
  apod(date: String): AstronomyPicture
  earthObservatory: EarthObservatoryPicture
}

type EarthObservatoryPicture {
  date: String!
  title: String!
  explanation: String!
  url: String!
  mediaType: String!
  credit: String
  copyright: String
  imageDate: String
  locationName: String
  latitude: Float
  longitude: Float
  articleUrl: String!
}
```

Resolver: make one server-side `GET /earth-observatory` request with the current request ID and W3C `traceparent` propagation, then map snake_case REST fields to GraphQL camelCase. Do not expose `urlFallback` or add EO records to APOD search/similarity.

## Next phase, intentionally not included

- EventBridge/cron-triggered daily EO refresh.
- Historical EO import from NASA Explorer/RSS/article data.
- S3 archival and checksum validation of EO media.
- EO search embeddings or similarity.
