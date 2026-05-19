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

-- Per-User Calendar-Token für /calendar/wipes.ics (Subscribe-URL für Apple/Google Calendar).
-- Token statt Cookie weil Calendar-Apps keine Session-Cookies senden.
-- NULL bedeutet "User hat noch keinen generiert" — wird lazy on-demand erstellt.
ALTER TABLE users ADD COLUMN IF NOT EXISTS calendar_token TEXT UNIQUE;

-- Game-Scores (Rust Flap Minigame auf der Site).
-- Pro Spieler eine Zeile pro Game-Over. Leaderboard zieht MAX(score) per User.
CREATE TABLE IF NOT EXISTS game_scores (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game        TEXT    NOT NULL,
    score       INTEGER NOT NULL,
    duration_ms INTEGER,
    played_at   BIGINT  NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_game_scores_user_game  ON game_scores(user_id, game, score DESC);
CREATE INDEX IF NOT EXISTS idx_game_scores_game_score ON game_scores(game, score DESC);

-- Ghost-Flag für seeded Demo-Spieler — werden im Leaderboard mitgezählt aber
-- aus User-Stats (admin /admin/stats) rausgefiltert. Real-User-Counts bleiben sauber.
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_ghost BOOLEAN NOT NULL DEFAULT FALSE;

-- Seed: 10 Ghost-Spieler mit beatbaren Scores (5..18). Realistische Flappy-Bird-
-- Zahlen — ein motivierter Spieler hackt den Top-Platz nach 3-5 Tries.
-- IDs 1_000_001..1_000_010 — weit unter real-Steam-Range (76_561_197+), keine
-- Kollisionsgefahr. ON CONFLICT macht's idempotent (init.sql ist immer re-runnable).
INSERT INTO users (id, display_name, avatar_url, profile_url, is_admin, is_ghost, created_at, last_login_at) VALUES
  (1000001, 'SoloRoamer',       NULL, NULL, FALSE, TRUE, EXTRACT(EPOCH FROM NOW() - INTERVAL '14 days')::bigint, EXTRACT(EPOCH FROM NOW() - INTERVAL '2 days')::bigint),
  (1000002, 'Bushcamper.exe',   NULL, NULL, FALSE, TRUE, EXTRACT(EPOCH FROM NOW() - INTERVAL '12 days')::bigint, EXTRACT(EPOCH FROM NOW() - INTERVAL '1 days')::bigint),
  (1000003, 'RoofCamp Steve',   NULL, NULL, FALSE, TRUE, EXTRACT(EPOCH FROM NOW() - INTERVAL '10 days')::bigint, EXTRACT(EPOCH FROM NOW() - INTERVAL '3 days')::bigint),
  (1000004, 'RaidAlertGod',     NULL, NULL, FALSE, TRUE, EXTRACT(EPOCH FROM NOW() - INTERVAL '9 days')::bigint,  EXTRACT(EPOCH FROM NOW() - INTERVAL '4 days')::bigint),
  (1000005, 'Hazmat_Diver',     NULL, NULL, FALSE, TRUE, EXTRACT(EPOCH FROM NOW() - INTERVAL '8 days')::bigint,  EXTRACT(EPOCH FROM NOW() - INTERVAL '2 days')::bigint),
  (1000006, 'C4-Karen',         NULL, NULL, FALSE, TRUE, EXTRACT(EPOCH FROM NOW() - INTERVAL '7 days')::bigint,  EXTRACT(EPOCH FROM NOW() - INTERVAL '5 days')::bigint),
  (1000007, 'SulfurGremlin',    NULL, NULL, FALSE, TRUE, EXTRACT(EPOCH FROM NOW() - INTERVAL '6 days')::bigint,  EXTRACT(EPOCH FROM NOW() - INTERVAL '1 days')::bigint),
  (1000008, 'ProneAndDone',     NULL, NULL, FALSE, TRUE, EXTRACT(EPOCH FROM NOW() - INTERVAL '5 days')::bigint,  EXTRACT(EPOCH FROM NOW() - INTERVAL '6 days')::bigint),
  (1000009, 'WallShareW',       NULL, NULL, FALSE, TRUE, EXTRACT(EPOCH FROM NOW() - INTERVAL '4 days')::bigint,  EXTRACT(EPOCH FROM NOW() - INTERVAL '3 days')::bigint),
  (1000010, 'FreshSpawnFranz',  NULL, NULL, FALSE, TRUE, EXTRACT(EPOCH FROM NOW() - INTERVAL '3 days')::bigint,  EXTRACT(EPOCH FROM NOW() - INTERVAL '1 days')::bigint)
ON CONFLICT (id) DO NOTHING;

INSERT INTO game_scores (user_id, game, score, duration_ms, played_at) VALUES
  (1000001, 'flap', 18, 27000, EXTRACT(EPOCH FROM NOW() - INTERVAL '2 days')::bigint),
  (1000002, 'flap', 16, 24000, EXTRACT(EPOCH FROM NOW() - INTERVAL '1 days')::bigint),
  (1000003, 'flap', 14, 21000, EXTRACT(EPOCH FROM NOW() - INTERVAL '3 days')::bigint),
  (1000004, 'flap', 13, 19500, EXTRACT(EPOCH FROM NOW() - INTERVAL '4 days')::bigint),
  (1000005, 'flap', 11, 17000, EXTRACT(EPOCH FROM NOW() - INTERVAL '2 days')::bigint),
  (1000006, 'flap', 10, 15500, EXTRACT(EPOCH FROM NOW() - INTERVAL '5 days')::bigint),
  (1000007, 'flap',  9, 14000, EXTRACT(EPOCH FROM NOW() - INTERVAL '1 days')::bigint),
  (1000008, 'flap',  8, 12500, EXTRACT(EPOCH FROM NOW() - INTERVAL '6 days')::bigint),
  (1000009, 'flap',  6, 10000, EXTRACT(EPOCH FROM NOW() - INTERVAL '3 days')::bigint),
  (1000010, 'flap',  5,  8500, EXTRACT(EPOCH FROM NOW() - INTERVAL '1 days')::bigint)
ON CONFLICT DO NOTHING;

-- Steam-gepflegte Rust-Lifetime-Stats (kommen aus GetUserStatsForGame).
-- Privacy-Status: privat = NULL in den Counter-Feldern, is_private = TRUE.
-- raw_json speichert den kompletten Steam-Response für künftige neue Counter
-- die wir noch nicht als Spalten haben.
CREATE TABLE IF NOT EXISTS rust_player_stats (
    steam_id            BIGINT  PRIMARY KEY,
    fetched_at          BIGINT  NOT NULL,
    is_private          BOOLEAN NOT NULL DEFAULT FALSE,
    error               TEXT,
    -- Core counters
    seconds_played      BIGINT,
    deaths              BIGINT,
    kill_player         BIGINT,
    headshot            BIGINT,
    wounded             BIGINT,
    -- Bullets
    bullet_fired        BIGINT,
    bullet_hit_player   BIGINT,
    bullet_hit_building BIGINT,
    bullet_hit_sign     BIGINT,
    bullet_hit_wolf     BIGINT,
    bullet_hit_bear     BIGINT,
    bullet_hit_boar     BIGINT,
    bullet_hit_stag     BIGINT,
    bullet_hit_horse    BIGINT,
    bullet_hit_corpse   BIGINT,
    -- Arrows
    arrow_fired         BIGINT,
    arrow_hit_player    BIGINT,
    arrow_hit_entity    BIGINT,
    -- Harvested raw resources
    harvested_wood      BIGINT,
    harvested_stones    BIGINT,
    harvested_cloth     BIGINT,
    harvested_leather   BIGINT,
    harvested_sulfur_ore BIGINT,
    harvested_metal_ore BIGINT,
    harvested_hq_metal_ore BIGINT,
    -- Acquired items
    acquired_scrap      BIGINT,
    acquired_lowgradefuel BIGINT,
    acquired_metalfrag  BIGINT,
    acquired_sulfur     BIGINT,
    -- Misc
    seconds_cold        BIGINT,
    seconds_hot         BIGINT,
    seconds_comfort     BIGINT,
    melee_thrown        BIGINT,
    c4_thrown           BIGINT,
    rocket_fired        BIGINT,
    -- Raw response for anything we didn't model as a column
    raw_json            TEXT
);
CREATE INDEX IF NOT EXISTS idx_rust_stats_fetched ON rust_player_stats(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_rust_stats_kills   ON rust_player_stats(kill_player DESC NULLS LAST);
