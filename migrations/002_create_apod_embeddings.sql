CREATE TABLE IF NOT EXISTS apod_embeddings (
    apod_date TEXT PRIMARY KEY
        REFERENCES apods(date) ON DELETE CASCADE,
    embedding F32_BLOB(3072) NOT NULL
);

CREATE INDEX IF NOT EXISTS apod_embeddings_vector_idx
ON apod_embeddings(libsql_vector_idx(embedding));
