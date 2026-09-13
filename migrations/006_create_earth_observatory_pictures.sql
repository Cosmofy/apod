CREATE TABLE IF NOT EXISTS earth_observatory_pictures (
    date TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    explanation TEXT NOT NULL,
    media_url TEXT NOT NULL,
    url_fallback TEXT,
    credit TEXT,
    copyright TEXT,
    article_url TEXT NOT NULL,
    image_date TEXT,
    location_name TEXT,
    latitude REAL,
    longitude REAL
);
