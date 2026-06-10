"""
Embeddings client — configurable OpenAI-compatible provider.

The Acme gateway does not currently expose an embeddings model, so the
provider is fully configurable and the whole RAG layer degrades gracefully
to keyword-only matching when no provider is set (embed() returns None).

Three ways to provide embeddings:
  1. api    — any OpenAI-compatible /embeddings endpoint (gateway or a local
              sidecar like text-embeddings-inference — no code change, just
              point api_base at it).
  2. local  — a small model loaded IN-PROCESS via fastembed (ONNX, no torch,
              CPU-fast). No external service. e.g. BAAI/bge-small-en-v1.5
              (384-dim). Lazy-imported so the base image isn't bloated.
  3. (unset) — keyword-only matching (graceful default).

Config (config.yaml):
  embeddings:
    provider: local                       # api | local  (default: api if api_base set)
    local_model: BAAI/bge-small-en-v1.5    # for provider=local
    dim: 384                               # MUST match the model (pgvector tables)
    # for provider=api:
    api_base: https://...
    api_key: ...
    model: text-embedding-3-small

Env overrides: VK_EMBEDDINGS_API_BASE / VK_EMBEDDINGS_API_KEY / VK_EMBEDDINGS_MODEL
               / VK_EMBEDDINGS_PROVIDER / VK_EMBEDDINGS_LOCAL_MODEL
"""
import json
import logging
import urllib.request

log = logging.getLogger(__name__)

MAX_INPUT_CHARS = 8000   # per text — truncate long incident analyses
BATCH_SIZE = 64


class EmbeddingClient:
    def __init__(self, api_base: str = "", api_key: str = "", model: str = "",
                 dim: int = 1536, provider: str = "", local_model: str = ""):
        self.api_base = (api_base or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.dim = dim
        self.local_model = local_model or ""
        # Infer provider: explicit > local if local_model set > api if api_base set
        self.provider = provider or ("local" if local_model else ("api" if self.api_base else ""))
        self._local = None   # lazy fastembed model

    @property
    def configured(self) -> bool:
        if self.provider == "local":
            return bool(self.local_model)
        return bool(self.api_base and self.model)

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """
        Embed texts. Returns None when unconfigured or on provider failure —
        callers treat None as 'no semantic leg, keyword-only'.
        """
        if not self.configured or not texts:
            return None
        if self.provider == "local":
            return self._embed_local(texts)
        return self._embed_api(texts)

    def _embed_api(self, texts: list[str]) -> list[list[float]] | None:
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
                rows = sorted(data["data"], key=lambda d: d["index"])
                out.extend(r["embedding"] for r in rows)
            return out
        except Exception as e:
            log.warning(f"Embeddings API failed ({type(e).__name__}: {str(e)[:80]}) — keyword-only")
            return None

    def _embed_local(self, texts: list[str]) -> list[list[float]] | None:
        try:
            model = self._get_local_model()
            if model is None:
                return None
            batch = [t[:MAX_INPUT_CHARS] for t in texts]
            # fastembed returns a generator of numpy arrays
            return [list(map(float, v)) for v in model.embed(batch)]
        except Exception as e:
            log.warning(f"Local embeddings failed ({type(e).__name__}: {str(e)[:80]}) — keyword-only")
            return None

    def _get_local_model(self):
        if self._local is not None:
            return self._local
        try:
            from fastembed import TextEmbedding
        except ImportError:
            log.warning("provider=local but `fastembed` not installed — keyword-only. "
                        "pip install fastembed")
            return None
        log.info(f"Loading local embedding model {self.local_model} (first call may download)")
        self._local = TextEmbedding(model_name=self.local_model)
        return self._local

    def embed_one(self, text: str) -> list[float] | None:
        vecs = self.embed([text])
        return vecs[0] if vecs else None


_client: EmbeddingClient | None = None


def init_embeddings(api_base: str = "", api_key: str = "", model: str = "",
                    dim: int = 1536, provider: str = "", local_model: str = "") -> None:
    global _client
    _client = EmbeddingClient(api_base, api_key, model, dim, provider, local_model)
    if _client.configured:
        if _client.provider == "local":
            log.info(f"Embeddings: local model {local_model} (dim {dim})")
        else:
            log.info(f"Embeddings: {model} @ {api_base}")
    else:
        log.info("Embeddings: not configured — RAG runs keyword-only")


def get_client() -> EmbeddingClient:
    global _client
    if _client is None:
        _client = EmbeddingClient()
    return _client
