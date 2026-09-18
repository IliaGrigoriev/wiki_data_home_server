CREATE TABLE IF NOT EXISTS entities (
    id text PRIMARY KEY,
    entity_type text NOT NULL,
    data jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS entities_entity_type_idx ON entities (entity_type);

CREATE TABLE IF NOT EXISTS wikipedia_pages (
    page_id bigint PRIMARY KEY,
    title text NOT NULL,
    namespace integer NOT NULL,
    redirect_title text,
    revision_id bigint,
    text text
);

CREATE INDEX IF NOT EXISTS wikipedia_pages_title_idx
    ON wikipedia_pages (namespace, title);

CREATE TABLE IF NOT EXISTS import_progress (
    source_key text PRIMARY KEY,
    source_description text NOT NULL,
    processed bigint NOT NULL DEFAULT 0 CHECK (processed >= 0),
    last_id text,
    decompressed_offset bigint,
    completed boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE import_progress ADD COLUMN IF NOT EXISTS completed boolean NOT NULL DEFAULT false;
ALTER TABLE import_progress ADD COLUMN IF NOT EXISTS decompressed_offset bigint;
