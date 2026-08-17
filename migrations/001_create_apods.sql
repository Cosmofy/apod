CREATE TABLE IF NOT EXISTS apods (
    date TEXT PRIMARY KEY,
    title TEXT,
    explanation TEXT,
    media_url TEXT,
    hd_media_url TEXT,
    media_type TEXT,
    credit TEXT,
    copyright TEXT
);