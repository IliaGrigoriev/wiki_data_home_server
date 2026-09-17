CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS entities (
    id text PRIMARY KEY,
    entity_type text NOT NULL,
    data jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS entities_entity_type_idx ON entities (entity_type);

CREATE TABLE IF NOT EXISTS import_progress (
    source_key text PRIMARY KEY,
    source_description text NOT NULL,
    processed bigint NOT NULL DEFAULT 0 CHECK (processed >= 0),
    last_id text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE import_progress ADD COLUMN IF NOT EXISTS last_id text;
