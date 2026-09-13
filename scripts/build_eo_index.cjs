const { createClient } = require('../data/eo-builder-node/node_modules/@libsql/client');
async function main() {
  const db = createClient({url: 'file:' + process.argv[2]});
  const started = Date.now();
  try {
    await db.execute('PRAGMA cache_size=-262144');
    await db.execute(`CREATE INDEX earth_observatory_embeddings_vector_idx
      ON earth_observatory_embeddings(libsql_vector_idx(embedding,
        'metric=cosine','max_neighbors=32','compress_neighbors=float8'))`);
    console.log(JSON.stringify({local_index_seconds:(Date.now()-started)/1000}));
    const sample = (await db.execute('SELECT embedding FROM earth_observatory_embeddings LIMIT 1')).rows[0].embedding;
    const found = await db.execute({sql:"SELECT * FROM vector_top_k('earth_observatory_embeddings_vector_idx',?,5)",args:[sample]});
    if (found.rows.length !== 5) throw new Error('vector_top_k check failed');
    const integrity = await db.execute('PRAGMA integrity_check');
    if (integrity.rows[0][0] !== 'ok') throw new Error('integrity check failed');
    await db.execute('PRAGMA journal_mode=WAL');
    await db.execute('PRAGMA wal_checkpoint(TRUNCATE)');
    console.log(JSON.stringify({matches:found.rows.length,integrity:'ok'}));
  } finally { db.close(); }
}
main().catch(error => {console.error(error.message);process.exitCode=1;});
