-- Ropi initial SQLite library schema (SPEC 6.5)

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(parent_id, name)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_folders_root_name
    ON folders(name)
    WHERE parent_id IS NULL;

CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY,
    folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    artist TEXT DEFAULT '',
    memo TEXT DEFAULT '',
    file_rel_path TEXT NOT NULL,
    format TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    duration_ms INTEGER,
    note_count INTEGER,
    bpm REAL,
    min_note INTEGER,
    max_note INTEGER,
    difficulty INTEGER,
    favorite INTEGER DEFAULT 0,
    play_count INTEGER DEFAULT 0,
    last_played_at TEXT,
    source_url TEXT,
    source_engine TEXT,
    settings_json TEXT DEFAULT '{}',
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_songs_folder_deleted ON songs(folder_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_songs_deleted ON songs(deleted_at);
CREATE INDEX IF NOT EXISTS idx_songs_hash ON songs(file_hash);
CREATE INDEX IF NOT EXISTS idx_songs_title ON songs(title);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    color TEXT
);

CREATE TABLE IF NOT EXISTS song_tags (
    song_id INTEGER REFERENCES songs(id) ON DELETE CASCADE,
    tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY(song_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_song_tags_tag ON song_tags(tag_id, song_id);

CREATE TABLE IF NOT EXISTS lyrics (
    id INTEGER PRIMARY KEY,
    song_id INTEGER REFERENCES songs(id) ON DELETE CASCADE,
    line_no INTEGER NOT NULL,
    time_ms INTEGER,
    text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lyrics_song ON lyrics(song_id, line_no);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    song_id INTEGER REFERENCES songs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_playlist_items_order ON playlist_items(playlist_id, position);

CREATE TABLE IF NOT EXISTS convert_jobs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    engine TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL DEFAULT 0,
    error TEXT,
    song_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS song_fts USING fts5(
    title,
    artist,
    tags,
    lyrics,
    chosung,
    tokenize='unicode61'
);
