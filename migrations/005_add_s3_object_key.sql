-- Set only after upload/checksum verification; preserve original NASA URLs.
ALTER TABLE apods ADD COLUMN s3_object_key TEXT;
