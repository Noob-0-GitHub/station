"""Memory: search over what the AI has written and lived through.

What is indexed (`memory.sources`):

- diary: each `## HH:MM` entry of ~/diary/YYYY-MM-DD.md
- chat: the conversation of every thread and every archived thread, a few messages per chunk
- note: things saved on purpose with `station memory add`

How:

- Text is cut into chunks, stored in `state/memory.db` (SQLite).
- Each chunk is sent to OpenAI's embeddings API (`memory.model`, through `station api`,
  so it is metered in credits) and comes back as a vector: `memory.dimensions` numbers
  that place the text by meaning, so texts about the same thing get nearby vectors.
- sqlite-vec keeps the vectors in the same database and finds the ones nearest to a
  query's vector (cosine distance).
- An FTS5 trigram index finds exact words and names, Chinese included, with no network.
- A search merges both rankings (reciprocal rank fusion).

Indexing runs when the AI falls asleep (bounded by `memory.sync_max_chunks`) and with
`station memory sync`. Unchanged files are not read again; a chunk whose text is
already indexed is not embedded again. The live end of a thread is left for later:
it is still in the AI's context.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import api, clock
from .ledger import transaction
from .runner import clip, safe_read

KINDS = ("diary", "chat", "note")
CHUNK_CHARS = 1200  # a conversation chunk is closed once it holds this many characters
PART_CHARS = 1500  # what one message contributes, at most
RESULT_CHARS = 300  # tool results are mostly noise; keep their start
ENTRY_CHARS = 2400  # a longer diary entry is split at paragraphs
BATCH_ITEMS = 64
BATCH_CHARS = 120_000
RRF_K = 60
MIN_REQUEST_SECONDS = 5  # a sync with less time left than this sends no more requests

STAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) [A-Z][a-z]{2} (\d{2}):(\d{2})")
ENTRY_RE = re.compile(r"^## (\d{2}):(\d{2})\s*$", re.M)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SPLIT_RE = re.compile(r"[\s,.;:!?，。；：！？、\"'()（）\[\]<>《》「」]+")


class MemError(RuntimeError):
    pass


@dataclass
class Chunk:
    kind: str
    title: str
    text: str
    ts: float | None

    @property
    def hash(self) -> str:
        return hashlib.sha256(f"{self.kind}\0{self.title}\0{self.text}".encode()).hexdigest()


# ---- cutting text into chunks -------------------------------------------------

def split_long(text: str, limit: int) -> list[str]:
    """Split at blank lines into pieces of at most `limit` characters (a longer paragraph is cut)."""
    pieces, cur = [], ""
    for para in re.split(r"\n\s*\n", text.strip()):
        while len(para) > limit:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(para[:limit])
            para = para[limit:]
        if cur and len(cur) + len(para) + 2 > limit:
            pieces.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur.strip():
        pieces.append(cur)
    return [p.strip() for p in pieces if p.strip()]


def diary_chunks(day: str, text: str) -> list[Chunk]:
    date = dt.date.fromisoformat(day)
    entries = list(ENTRY_RE.finditer(text))
    if not entries:
        body = re.sub(r"^# .*\n", "", text, count=1).strip()
        return [Chunk("diary", f"diary {day}", p, clock.wall(date, 0, 0)) for p in split_long(body, ENTRY_CHARS)]
    out = []
    for i, m in enumerate(entries):
        end = entries[i + 1].start() if i + 1 < len(entries) else len(text)
        hh, mm = int(m.group(1)), int(m.group(2))
        ts = clock.wall(date, hh, mm) if hh < 24 and mm < 60 else clock.wall(date, 0, 0)
        for piece in split_long(text[m.end():end], ENTRY_CHARS):
            out.append(Chunk("diary", f"diary {day} {hh:02d}:{mm:02d}", piece, ts))
    return out


def stamp_ts(m: re.Match) -> float | None:
    try:
        return clock.wall(dt.date.fromisoformat(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def message_text(m: dict) -> str:
    content = m.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
    parts = []
    for b in blocks:
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", "").replace("<env>", "").replace("</env>", "").strip())
        elif t == "tool_use":
            parts.append(f"[{b.get('name')}] " + clip(json.dumps(b.get("input"), ensure_ascii=False), PART_CHARS))
        elif t == "tool_result":
            c = b.get("content")
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
            c = str(c or "").strip()
            if c:
                parts.append("[result] " + clip(c, RESULT_CHARS))
        # reasoning blocks are opaque; images are not text
    text = "\n".join(p for p in parts if p)
    if not text:
        return ""
    who = "me" if m.get("role") == "assistant" else "station"
    return f"{who}: {clip(text, PART_CHARS)}"


def chat_chunks(tid: str, messages: list[dict], final: bool) -> list[Chunk]:
    """Consecutive messages grouped into chunks of about CHUNK_CHARS. Without `final`,
    the last unfinished chunk is left out (it is the part still in the AI's context)."""
    title = f"conversation, thread {tid}"
    out: list[Chunk] = []
    buf: list[str] = []
    size = 0
    last: float | None = None
    start: float | None = None
    for m in messages:
        text = message_text(m)
        stamps = [t for t in (stamp_ts(s) for s in STAMP_RE.finditer(text)) if t]
        if text and not buf:
            start = stamps[0] if stamps else last
        if stamps:
            last = stamps[-1]
        if not text:
            continue
        buf.append(text)
        size += len(text)
        if size >= CHUNK_CHARS:
            out.append(Chunk("chat", title, "\n".join(buf), start))
            buf, size = [], 0
    if buf and final:
        out.append(Chunk("chat", title, "\n".join(buf), start))
    return out


def read_jsonl(path: Path) -> list[dict]:
    """A thread's messages, each once. A thread file keeps its whole history: after each
    compaction marker comes the new conversation, whose first `carried` messages repeat
    (or summarize) what was already there, so they are skipped."""
    rows, skip = [], 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a line cut by a crash: skip it, keep the rest
        if "compaction" in row:
            skip = int((row["compaction"] or {}).get("carried") or 0)
        elif skip:
            skip -= 1
        else:
            rows.append(row)
    return rows


# ---- the index --------------------------------------------------------------------

# Vectors live in one table per model and size (`vectors_<dims>_<model hash>`), so a config
# change never mixes vectors of two models: a sync still running for the old config writes
# only to the old table. Tables of other configs are dropped by the next sync.
# Chunk ids are AUTOINCREMENT, never reused, so an id always names the same memory.
SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    ts REAL,
    title TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS places (source TEXT NOT NULL, hash TEXT NOT NULL, PRIMARY KEY (source, hash));
CREATE INDEX IF NOT EXISTS places_hash ON places (hash);
CREATE TABLE IF NOT EXISTS sources (source TEXT PRIMARY KEY, stamp TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(text, tokenize = 'trigram');
"""


def check_vectors(body, count: int, dims: int) -> list[list[float]]:
    """The vectors of an embeddings response, in input order; MemError unless every one is sound."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) != count:
        raise MemError("the embeddings API returned the wrong number of vectors")
    out: list = [None] * count
    for d in data:
        i = d.get("index") if isinstance(d, dict) else None
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < count or out[i] is not None:
            raise MemError("the embeddings API returned bad or repeated indices")
        v = d.get("embedding")
        if not isinstance(v, list) or len(v) != dims:
            raise MemError("the embeddings API returned a vector of the wrong size")
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v):
            raise MemError("the embeddings API returned a vector that is not all finite numbers")
        if not any(v):
            raise MemError("the embeddings API returned an all-zero vector")
        out[i] = [float(x) for x in v]
    return out


class Memory:
    def __init__(self, root, cfg: dict, ledger, home=None):
        self.root = Path(root)
        self.cfg = cfg
        self.mcfg = cfg["memory"]
        self.ledger = ledger
        self.home = Path(home) if home else (
            Path(cfg["home"]).expanduser() if cfg.get("home") else self.root / "home")
        self.state = self.root / "state"
        self.path = self.state / "memory.db"
        self.dims = int(self.mcfg["dimensions"])
        if not 1 <= self.dims <= 8192:
            raise ValueError("memory.dimensions must be between 1 and 8192")
        self.kinds = [k for k in KINDS if k in self.mcfg["sources"]]
        self.vt = f"vectors_{self.dims}_{hashlib.sha256(self.mcfg['model'].encode()).hexdigest()[:10]}"
        self.state.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            with transaction(db):
                for stmt in filter(str.strip, SCHEMA.split(";")):
                    db.execute(stmt)
                db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.vt} USING vec0(id INTEGER PRIMARY KEY, "
                           f"embedding float[{self.dims}] distance_metric=cosine, kind TEXT, ts FLOAT)")

    def connect(self) -> sqlite3.Connection:
        try:
            import sqlite_vec
        except ImportError:
            raise MemError("sqlite-vec is not installed (pip install sqlite-vec)") from None
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout = 30000")
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def locked(self, wait: bool = True):
        """One writer at a time across processes. Without `wait`, yields False if busy."""
        with open(self.state / "memory.lock", "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            yield True

    # ---- sources ---------------------------------------------------------

    def sources(self) -> dict[str, Path]:
        kinds = set(self.mcfg["sources"])
        out: dict[str, Path] = {}
        if "diary" in kinds and (self.home / "diary").is_dir():
            for p in sorted((self.home / "diary").glob("*.md")):
                if DATE_RE.match(p.stem):
                    out[f"diary/{p.name}"] = p
        if "chat" in kinds:
            for p in sorted((self.state / "threads").glob("*.jsonl")):
                out[f"thread/{p.stem}"] = p
            for p in sorted((self.state / "archive").glob("*.jsonl")):
                out[f"archive/{p.name}"] = p
        return out

    def parse(self, source: str, path: Path) -> list[Chunk]:
        kind, _, name = source.partition("/")
        if kind == "diary":
            return diary_chunks(path.stem, safe_read(path, self.home))
        if kind == "thread":
            return chat_chunks(name, read_jsonl(path), final=False)
        if kind == "archive":
            return chat_chunks(name.split("-")[0], read_jsonl(path), final=True)
        raise ValueError(f"unknown source {source}")

    # ---- writing ---------------------------------------------------------

    def _put(self, db, chunk: Chunk, source: str) -> int:
        row = db.execute("SELECT id FROM chunks WHERE hash = ?", (chunk.hash,)).fetchone()
        if row is None:
            cid = db.execute("INSERT INTO chunks (hash, kind, ts, title, text) VALUES (?, ?, ?, ?, ?)",
                             (chunk.hash, chunk.kind, chunk.ts, chunk.title, chunk.text)).lastrowid
            db.execute("INSERT INTO chunk_fts (rowid, text) VALUES (?, ?)", (cid, f"{chunk.title}\n{chunk.text}"))
        else:
            cid = row[0]
        db.execute("INSERT OR IGNORE INTO places (source, hash) VALUES (?, ?)", (source, chunk.hash))
        return cid

    def _gc(self, db) -> int:
        dead = [r[0] for r in db.execute("SELECT id FROM chunks WHERE hash NOT IN (SELECT hash FROM places)")]
        for cid in dead:
            db.execute("DELETE FROM chunks WHERE id = ?", (cid,))
            db.execute("DELETE FROM chunk_fts WHERE rowid = ?", (cid,))
            db.execute(f"DELETE FROM {self.vt} WHERE id = ?", (cid,))
        return len(dead)

    def _drop_other_vectors(self, db) -> None:
        """Vector tables of earlier models or sizes. Only called with the writer lock held."""
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE "
                                          "'CREATE VIRTUAL TABLE%vec0%' AND name != ?", (self.vt,))]
        for name in names:
            if re.fullmatch(r"vectors(_\d+_[0-9a-f]{10})?", name):
                db.execute(f"DROP TABLE {name}")

    def _scan(self, db, deadline: float | None) -> dict:
        """Re-read changed files. Files are re-derivable, so a diary or thread whose kind was
        switched off is dropped; notes exist only here and are kept (just not searched)."""
        known = {r[0]: r[1] for r in db.execute("SELECT source, stamp FROM sources")}
        current = self.sources()
        changed, failed, left = 0, [], 0
        with transaction(db):
            self._drop_other_vectors(db)
            for source in set(known) - set(current):  # deleted, or its kind was switched off
                db.execute("DELETE FROM places WHERE source = ?", (source,))
                db.execute("DELETE FROM sources WHERE source = ?", (source,))
            for source, path in current.items():
                if deadline and time.time() > deadline:
                    left += 1  # not stamped, so the next sync reads it
                    continue
                try:
                    st = path.stat()
                    stamp = f"{st.st_mtime_ns}:{st.st_size}"
                    if known.get(source) == stamp:
                        continue
                    chunks = self.parse(source, path)
                except (OSError, ValueError) as e:
                    failed.append(f"{source}: {e}")
                    continue
                db.execute("DELETE FROM places WHERE source = ?", (source,))
                for c in chunks:
                    self._put(db, c, source)
                db.execute("INSERT OR REPLACE INTO sources VALUES (?, ?)", (source, stamp))
                changed += 1
            removed = self._gc(db)
        return {"changed": changed, "removed": removed, "failed": failed, "unread": left}

    def embed(self, texts: list[str], what: str, timeout: float = 120) -> list[list[float]]:
        r = api.request(self.cfg, self.ledger, "openai", "POST", "/v1/embeddings", what=what, timeout=timeout,
                        json_body={"model": self.mcfg["model"], "input": texts, "dimensions": self.dims})
        try:
            return check_vectors(r.json(), len(texts), self.dims)
        except MemError:
            raise
        except Exception:  # any shape we did not expect: never store it
            raise MemError("the embeddings API returned something unexpected") from None

    def _embed_pending(self, db, max_chunks: int, deadline: float | None) -> tuple[int, str | None]:
        import sqlite_vec

        if not self.kinds:
            return 0, None
        marks = ",".join("?" * len(self.kinds))
        rows = db.execute(f"SELECT id, kind, ts, title, text FROM chunks WHERE kind IN ({marks}) "
                          f"AND id NOT IN (SELECT id FROM {self.vt}) "
                          "ORDER BY COALESCE(ts, 0) DESC, id DESC LIMIT ?", (*self.kinds, max_chunks)).fetchall()
        done = 0
        while rows:
            batch, chars = [], 0
            while rows and len(batch) < BATCH_ITEMS and chars + len(rows[0]["text"]) < BATCH_CHARS:
                chars += len(rows[0]["text"])
                batch.append(rows.pop(0))
            if not batch:
                batch.append(rows.pop(0))
            timeout = 120.0
            if deadline:
                timeout = min(timeout, deadline - time.time())
                if timeout < MIN_REQUEST_SECONDS:
                    break
            try:
                vectors = self.embed([f"{r['title']}\n{r['text']}" for r in batch],
                                     what=f"memory index: {len(batch)} chunks", timeout=timeout)
            except (api.ApiError, MemError) as e:
                return done, str(e)
            with transaction(db):
                for r, v in zip(batch, vectors):
                    if db.execute("SELECT 1 FROM chunks WHERE id = ?", (r["id"],)).fetchone() is None:
                        continue  # removed while the request was in flight
                    db.execute(f"DELETE FROM {self.vt} WHERE id = ?", (r["id"],))
                    db.execute(f"INSERT INTO {self.vt} (id, embedding, kind, ts) VALUES (?, ?, ?, ?)",
                               (r["id"], sqlite_vec.serialize_float32(v), r["kind"], float(r["ts"] or 0)))
            done += len(batch)
        return done, None

    def sync(self, max_chunks: int | None = None, budget: float | None = None, wait: bool = False) -> dict:
        """Read changed sources, then embed up to `max_chunks` new chunks (newest first)."""
        with self.locked(wait) as got:
            if not got:
                return {"busy": True}
            deadline = time.time() + budget if budget else None
            with closing(self.connect()) as db:
                scan = self._scan(db, deadline)
                embedded, error = self._embed_pending(db, max_chunks or int(self.mcfg["sync_max_chunks"]), deadline)
                pending = self._pending(db)
        return {**scan, "embedded": embedded, "pending": pending, "error": error}

    def _pending(self, db) -> int:
        if not self.kinds:
            return 0
        return db.execute(f"SELECT COUNT(*) FROM chunks WHERE kind IN ({','.join('?' * len(self.kinds))}) "
                          f"AND id NOT IN (SELECT id FROM {self.vt})", self.kinds).fetchone()[0]

    def add_note(self, text: str) -> int:
        text = text.strip()
        if not text:
            raise ValueError("the note is empty")
        if "note" not in self.mcfg["sources"]:
            raise ValueError("notes are switched off (memory.sources)")
        with self.locked(), closing(self.connect()) as db:
            with transaction(db):
                cid = self._put(db, Chunk("note", "note", clip(text, ENTRY_CHARS * 2), time.time()),
                                f"note:{uuid.uuid4().hex[:12]}")
            self._embed_pending(db, 1, time.time() + 30)  # searchable by meaning at once, if the API answers
        return cid

    def forget(self, cid: int) -> None:
        with self.locked(), closing(self.connect()) as db:
            row = db.execute("SELECT kind, hash FROM chunks WHERE id = ?", (cid,)).fetchone()
            if row is None:
                raise ValueError(f"no memory {cid}")
            if row["kind"] != "note":
                raise ValueError("only notes can be forgotten; diary entries and conversations follow their files")
            with transaction(db):
                db.execute("DELETE FROM places WHERE hash = ? AND source LIKE 'note:%'", (row["hash"],))
                self._gc(db)

    # ---- reading ---------------------------------------------------------

    def _keyword(self, db, query: str, n: int, kinds: list[str], since: float | None) -> list[int]:
        terms = [t for t in SPLIT_RE.split(query) if t]
        long_terms = [t for t in terms if len(t) >= 3]
        short = [t for t in terms if len(t) == 2]
        where = f" AND c.kind IN ({','.join('?' * len(kinds))})"
        args: list = list(kinds)
        if since is not None:
            where += " AND c.ts >= ?"
            args.append(since)
        ids: list[int] = []
        if long_terms:
            expr = " OR ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
            ids += [r[0] for r in db.execute(
                f"SELECT c.id FROM chunk_fts f JOIN chunks c ON c.id = f.rowid WHERE chunk_fts MATCH ?{where} "
                f"ORDER BY f.rank LIMIT ?", [expr, *args, n])]
        if short:  # trigrams need 3 characters; two-character words (common in Chinese) use LIKE
            like = " OR ".join("c.text LIKE ? ESCAPE '\\'" for _ in short)
            pats = ["%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for t in short]
            ids += [r[0] for r in db.execute(
                f"SELECT c.id FROM chunks c WHERE ({like}){where} ORDER BY c.ts DESC LIMIT ?", [*pats, *args, n])
                    if r[0] not in ids]
        return ids

    def has_chunks(self) -> bool:
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None

    def search(self, query: str, limit: int = 8, kind: str | None = None,
               since: float | None = None, timeout: float = 30) -> tuple[list[dict], str | None]:
        import sqlite_vec

        query = query.strip()
        if not query:
            raise ValueError("the query is empty")
        if kind and kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if kind and kind not in self.kinds:
            raise ValueError(f"{kind} is not in memory.sources")
        kinds = [kind] if kind else self.kinds
        if not kinds:
            return [], "memory.sources is empty"
        score: dict[int, float] = {}
        how: dict[int, set] = {}

        def add(cid: int, rank: int, why: str) -> None:
            score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
            how.setdefault(cid, set()).add(why)

        limit = max(1, min(int(limit), 100))
        note, vec = None, None
        try:  # the network call, before any read: the reads below then see one snapshot
            vec = self.embed([query[:8000]], what="memory search", timeout=timeout)[0]
        except (api.ApiError, MemError) as e:
            note = f"search by meaning is unavailable ({e}); these are word matches only"
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            try:
                if vec is not None:
                    sql = (f"SELECT id FROM {self.vt} WHERE embedding MATCH ? AND k = ? "
                           f"AND kind IN ({','.join('?' * len(kinds))})")
                    args: list = [sqlite_vec.serialize_float32(vec), limit * 4, *kinds]
                    if since is not None:
                        sql += " AND ts >= ?"
                        args.append(since)
                    for rank, row in enumerate(db.execute(sql + " ORDER BY distance", args)):
                        add(row[0], rank, "meaning")
                for rank, cid in enumerate(self._keyword(db, query, limit * 4, kinds, since)):
                    add(cid, rank, "words")
                ids = sorted(score, key=lambda c: -score[c])[:limit]
                rows = {r["id"]: dict(r) for r in db.execute(
                    f"SELECT id, kind, ts, title, text FROM chunks WHERE id IN ({','.join('?' * len(ids))})",
                    ids)} if ids else {}
            finally:
                db.execute("COMMIT")
        return [{**rows[c], "how": sorted(how[c])} for c in ids if c in rows], note

    def show(self, cid: int) -> dict:
        with closing(self.connect()) as db:
            row = db.execute(f"SELECT id, kind, ts, title, text, id IN (SELECT id FROM {self.vt}) AS embedded "
                             "FROM chunks WHERE id = ?", (cid,)).fetchone()
            if row is None:
                raise ValueError(f"no memory {cid}")
            places = [r[0] for r in db.execute("SELECT p.source FROM places p JOIN chunks c ON c.hash = p.hash "
                                               "WHERE c.id = ?", (cid,))]
        return {**dict(row), "sources": places}

    def status(self) -> dict:
        with closing(self.connect()) as db:
            kinds = {r[0]: {"chunks": r[1], "pending": r[2] if r[0] in self.kinds else 0} for r in db.execute(
                f"SELECT kind, COUNT(*), SUM(id NOT IN (SELECT id FROM {self.vt})) FROM chunks GROUP BY kind")}
            sources = db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        size = sum(p.stat().st_size for p in self.state.glob("memory.db*") if p.is_file())
        return {"kinds": kinds, "sources": sources, "bytes": size, "model": self.mcfg["model"],
                "dimensions": self.dims, "indexed": list(self.mcfg["sources"])}
