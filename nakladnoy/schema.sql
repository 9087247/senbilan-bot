CREATE TABLE IF NOT EXISTS counterparties (
    id       SERIAL PRIMARY KEY,
    name     TEXT NOT NULL,
    inn      TEXT NOT NULL UNIQUE,
    contract TEXT,
    comment  TEXT
);

CREATE TABLE IF NOT EXISTS exports (
    id         SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id    BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id              SERIAL PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'confirmed', 'exported', 'cancelled')),
    doc_date        DATE,
    supplier        TEXT NOT NULL DEFAULT '',
    receiver        TEXT NOT NULL DEFAULT '',
    total           NUMERIC(18, 2),
    unclear         TEXT[] NOT NULL DEFAULT '{}',
    counterparty_id INTEGER REFERENCES counterparties (id),
    photo_file_id   TEXT,
    user_id         BIGINT NOT NULL,
    export_id       INTEGER REFERENCES exports (id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS documents_status_idx ON documents (status);

CREATE TABLE IF NOT EXISTS document_items (
    document_id INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    line_no     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT '',
    qty         NUMERIC(18, 3),
    price       NUMERIC(18, 2),
    amount      NUMERIC(18, 2),
    PRIMARY KEY (document_id, line_no)
);
