CREATE TABLE IF NOT EXISTS earth_observatory_embeddings (
    picture_date TEXT PRIMARY KEY
        REFERENCES earth_observatory_pictures(date) ON DELETE CASCADE,
    embedding F32_BLOB(3072) NOT NULL
);

CREATE INDEX IF NOT EXISTS earth_observatory_embeddings_vector_idx
ON earth_observatory_embeddings(
    libsql_vector_idx(
        embedding,
        'metric=cosine',
        'max_neighbors=32',
        'compress_neighbors=float8'
    )
);
