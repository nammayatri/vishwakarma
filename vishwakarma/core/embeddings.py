"""
Embeddings client — configurable OpenAI-compatible provider.

The Acme gateway does not currently expose an embeddings model, so the
provider is fully configurable and the whole RAG layer degrades gracefully
to keyword-only matching when no provider is set (embed() returns None).

Config (config.yaml):
  embeddings:
    api_base: https://api.openai.com/v1     # or any OpenAI-compatible endpoint
    api_key: sk-...
    model: text-embedding-3-small
    dim: 1536

Env overrides: VK_EMBEDDINGS_API_BASE / VK_EMBEDDINGS_API_KEY / VK_EMBEDDINGS_MODEL
"""
import json
import logging
import urllib.request

log = logging.getLogger(__name__)

MAX_INPUT_CHARS = 8000   # per text — truncate long incident analyses
BATCH_SIZE = 64


class EmbeddingClient:
    def __init__(self, api_base: str = "", api_key: str = "", model: str = "", dim: int = 1536):
        self.api_base = (api_base or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.dim = dim

    @property
    def configured(self) -> bool:
        return bool(self.api_base and self.model)

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """
        Embed texts. Returns None when unconfigured or on provider failure —
        callers treat None as 'no semantic leg, keyword-only'.
        """
        if not self.configured or not texts:
            return None
        out: list[list[float]] = []
        try:
            for i in range(0, len(texts), BATCH_SIZE):
                batch = [t[:MAX_INPUT_CHARS] for t in texts[i:i + BATCH_SIZE]]
                req = urllib.request.Request(
                    f"{self.api_base}/embeddings",
                    data=json.dumps({"model": self.model, "input": batch}).encode(),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                # OpenAI shape: {"data": [{"index": n, "embedding": [...]}, ...]}
                rows = sorted(data["data"], key=lambda d: d["index"])
                out.extend(r["embedding"] for r in rows)
            return out
        except Exception as e:
            log.warning(f"Embeddings provider failed ({type(e).__name__}: {str(e)[:80]}) — degrading to keyword-only")
            return None

    def embed_one(self, text: str) -> list[float] | None:
        vecs = self.embed([text])
        return vecs[0] if vecs else None


_client: EmbeddingClient | None = None


def init_embeddings(api_base: str = "", api_key: str = "", model: str = "", dim: int = 1536) -> None:
    global _client
    _client = EmbeddingClient(api_base, api_key, model, dim)
    if _client.configured:
        log.info(f"Embeddings: {model} @ {api_base}")
    else:
        log.info("Embeddings: not configured — RAG runs keyword-only")


def get_client() -> EmbeddingClient:
    global _client
    if _client is None:
        _client = EmbeddingClient()
    return _client
