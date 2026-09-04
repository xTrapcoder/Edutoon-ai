-- EduToon AI — Postgres bootstrap
-- Runs once on first container start against the edutoon database.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
