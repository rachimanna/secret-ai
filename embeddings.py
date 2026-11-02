"""Search by meaning. Optional: it works once an embeddings key is configured; without one the word search stays."""
import array
import hashlib
import logging
import math

LOG = logging.getLogger(__name__)

SIZE = 256  # shortened vectors: a tenth of the storage, nearly the same search quality
KEEP = 20000  # about 20 MB; the database snapshot has a size limit
BATCH = 64
MAX_TEXTS = 300
MAX_CHARS = 2000
MIN_SIMILARITY = 0.25


def normalize(vector):
    short = list(vector)[:SIZE]
    size = math.sqrt(sum(x * x for x in short)) or 1.0
    return [x / size for x in short]


def similarity(a, b):
    return sum(x * y for x, y in zip(a, b))


class Embeddings:
    def __init__(self, db, client, model):
        self.db, self.client, self.model = db, client, model
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS embed_cache (id INTEGER PRIMARY KEY, hash TEXT NOT NULL UNIQUE, vector BLOB NOT NULL)")

    def _key(self, text):
        return hashlib.sha256(f"{self.model}\n{text}".encode()).hexdigest()

    def _stored(self, key):
        row = self.db.execute("SELECT vector FROM embed_cache WHERE hash=?", (key,)).fetchone()
        if not row:
            return None
        values = array.array("f")
        values.frombytes(row[0])
        return list(values)

    def _store(self, key, vector):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO embed_cache(hash, vector) VALUES (?, ?)", (key, array.array("f", vector).tobytes()))
            self.db.execute("DELETE FROM embed_cache WHERE id<=(SELECT MAX(id) FROM embed_cache)-?", (KEEP,))

    async def vectors(self, texts):
        """A vector for each text; known ones aren't paid for twice."""
        result, missing = {}, {}
        for text in texts:
            key = self._key(text)
            known = self._stored(key)
            if known is not None:
                result[text] = known
            else:
                missing[text] = key
        pending = list(missing)
        for start in range(0, len(pending), BATCH):
            part = pending[start:start + BATCH]
            answer = await self.client.embeddings.create(model=self.model, input=part)
            for text, item in zip(part, answer.data):
                vector = normalize(item.embedding)
                result[text] = vector
                self._store(missing[text], vector)
        return result

    async def rank(self, query, texts, limit=8):
        """[(index, similarity)] of the closest texts, best first. Empty when the service fails: the word search still stands."""
        clipped = [text[:MAX_CHARS] for text in texts[:MAX_TEXTS]]
        try:
            vectors = await self.vectors(list(dict.fromkeys(clipped + [query[:MAX_CHARS]])))
        except Exception as exc:
            LOG.warning("Embeddings unavailable: %s", type(exc).__name__)
            return []
        target = vectors[query[:MAX_CHARS]]
        scored = [(index, similarity(target, vectors[text])) for index, text in enumerate(clipped)]
        scored = [item for item in scored if item[1] >= MIN_SIMILARITY]
        return sorted(scored, key=lambda item: -item[1])[:limit]
