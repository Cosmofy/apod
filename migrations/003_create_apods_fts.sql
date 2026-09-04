CREATE VIRTUAL TABLE IF NOT EXISTS apods_fts USING fts5(
    title,
    explanation,
    credit,
    content='apods',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS apods_fts_after_insert
AFTER INSERT ON apods BEGIN
    INSERT INTO apods_fts(rowid, title, explanation, credit)
    VALUES (new.rowid, new.title, new.explanation, new.credit);
END;

CREATE TRIGGER IF NOT EXISTS apods_fts_after_delete
AFTER DELETE ON apods BEGIN
    INSERT INTO apods_fts(apods_fts, rowid, title, explanation, credit)
    VALUES ('delete', old.rowid, old.title, old.explanation, old.credit);
END;

CREATE TRIGGER IF NOT EXISTS apods_fts_after_update
AFTER UPDATE ON apods BEGIN
    INSERT INTO apods_fts(apods_fts, rowid, title, explanation, credit)
    VALUES ('delete', old.rowid, old.title, old.explanation, old.credit);
    INSERT INTO apods_fts(rowid, title, explanation, credit)
    VALUES (new.rowid, new.title, new.explanation, new.credit);
END;

INSERT INTO apods_fts(apods_fts) VALUES ('rebuild');
