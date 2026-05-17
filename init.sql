-- RustMetrics-Schema für Postgres
-- Wird einmalig beim Setup ausgeführt. Idempotent: kann mehrfach laufen.

-- Users werden über ihre SteamID64 identifiziert (BIGINT)
CREATE TABLE IF NOT EXISTS users (
    id              BIGINT PRIMARY KEY,
    display_name    TEXT,
    avatar_url      TEXT,
    profile_url     TEXT,
    is_admin        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      BIGINT      NOT NULL,
    last_login_at   BIGINT      NOT NULL
);

-- Sessions — opake Token-Cookies, gemappt auf User
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  BIGINT NOT NULL,
    expires_at  BIGINT NOT NULL,
    ip          TEXT,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- Globale Server-Tabelle (alle User teilen sich diese Daten — wir pollen jeden Server nur einmal)
CREATE TABLE IF NOT EXISTS servers (
    id          SERIAL PRIMARY KEY,
    host        TEXT    NOT NULL,
    port        INTEGER NOT NULL,
    name        TEXT,
    first_seen  BIGINT  NOT NULL,
    UNIQUE(host, port)
);

-- A2S-Snapshots (auch global, 1× pro Server pro Poll-Runde)
CREATE TABLE IF NOT EXISTS snapshots (
    id              BIGSERIAL PRIMARY KEY,
    server_id       INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    ts              BIGINT  NOT NULL,
    online          BOOLEAN NOT NULL,
    name            TEXT,
    map             TEXT,
    players_count   INTEGER,
    max_players     INTEGER,
    ping_ms         INTEGER,
    keywords        TEXT,
    players_json    TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_server_ts ON snapshots(server_id, ts);

-- Pro User: welche Server beobachtet er
CREATE TABLE IF NOT EXISTS watched_servers (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    server_id   INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    added_at    BIGINT  NOT NULL,
    UNIQUE(user_id, server_id)
);
CREATE INDEX IF NOT EXISTS idx_watched_servers_user ON watched_servers(user_id);

-- Pro User: welche Spielernamen auf welchem Server
CREATE TABLE IF NOT EXISTS watched_players (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    server_id   INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    added_at    BIGINT  NOT NULL,
    UNIQUE(user_id, server_id, name)
);
CREATE INDEX IF NOT EXISTS idx_watched_players_user ON watched_players(user_id);

-- Player-Sessions (global): wann erschien ein Name auf welchem Server, wann verschwand er
-- Wird per A2S-Diff gepflegt, alle User profitieren davon.
CREATE TABLE IF NOT EXISTS player_sessions (
    id           BIGSERIAL PRIMARY KEY,
    server_id    INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    player_name  TEXT    NOT NULL,
    start_ts     BIGINT  NOT NULL,
    end_ts       BIGINT
);
CREATE INDEX IF NOT EXISTS idx_psessions_player ON player_sessions(server_id, player_name);
CREATE INDEX IF NOT EXISTS idx_psessions_open   ON player_sessions(server_id, end_ts);

-- Auf welche Server hat dieser Server-Pool aktuell mindestens einen Watcher?
-- View damit der Poller nur "interessante" Server pollt (Effizienz).
CREATE OR REPLACE VIEW v_active_servers AS
SELECT s.* FROM servers s
WHERE EXISTS (
    SELECT 1 FROM watched_servers w WHERE w.server_id = s.id
);

-- Player-Metadaten (gepflegt aus BM-Player-Lookups).
-- bm_id ist stable über Namenswechsel, current_name ist der aktuellste bekannte Name,
-- aliases enthält frühere Namen, steam_id optional (wenn BM ihn public freigibt).
CREATE TABLE IF NOT EXISTS players (
    bm_id        BIGINT PRIMARY KEY,
    current_name TEXT   NOT NULL,
    aliases      TEXT[] NOT NULL DEFAULT '{}',
    steam_id     BIGINT,
    first_seen   BIGINT NOT NULL,
    last_seen    BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_players_steam ON players(steam_id) WHERE steam_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_players_last_seen ON players(last_seen DESC);

-- player_sessions: optional bm_player_id zum stable Tracking (wird durch Poller
-- gesetzt sobald BM-Daten verfügbar sind — alte Sessions bleiben NULL und werden
-- per Namens-Lookup gegen players.aliases gematcht).
ALTER TABLE player_sessions ADD COLUMN IF NOT EXISTS bm_player_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_psessions_bmid ON player_sessions(bm_player_id, start_ts DESC) WHERE bm_player_id IS NOT NULL;
