-- Set only after upload/checksum verification; preserve original NASA URL in url_fallback.
ALTER TABLE earth_observatory_pictures ADD COLUMN s3_object_key TEXT;
ALTER TABLE earth_observatory_pictures ADD COLUMN s3_url TEXT;
ALTER TABLE earth_observatory_pictures ADD COLUMN archive_sha256 TEXT;
ALTER TABLE earth_observatory_pictures ADD COLUMN archive_content_length INTEGER;
ALTER TABLE earth_observatory_pictures ADD COLUMN archive_content_type TEXT;
