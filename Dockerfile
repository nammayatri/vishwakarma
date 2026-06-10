FROM --platform=linux/amd64 python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ── Console UI build stage ────────────────────────────────────────────────────
FROM --platform=linux/amd64 node:20-slim AS webbuilder

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM --platform=linux/amd64 python:3.11-slim

ENV PYTHONUNBUFFERED=1

# System dependencies: WeasyPrint (PDF) + tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libgdk-pixbuf-2.0-0 \
    libffi8 \
    libcairo2 \
    shared-mime-info \
    fonts-liberation \
    curl \
    unzip \
    jq \
    git \
    gnupg \
    ripgrep \
    redis-tools \
    postgresql-client \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETPLATFORM

# kubectl (official k8s binary)
RUN ARCH=$([ "$TARGETPLATFORM" = "linux/arm64" ] && echo "arm64" || echo "amd64") && \
    curl -fsSLo /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/${ARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# stern (multi-pod log tailing) — pinned version
ARG STERN_VERSION=1.32.0
RUN ARCH=$([ "$TARGETPLATFORM" = "linux/arm64" ] && echo "arm64" || echo "amd64") && \
    curl -fsSL "https://github.com/stern/stern/releases/download/v${STERN_VERSION}/stern_${STERN_VERSION}_linux_${ARCH}.tar.gz" \
    -o /tmp/stern.tar.gz \
    && tar -xzf /tmp/stern.tar.gz -C /usr/local/bin stern \
    && rm /tmp/stern.tar.gz \
    && chmod +x /usr/local/bin/stern

# AWS CLI v2 (arch-aware)
RUN ARCH=$([ "$TARGETPLATFORM" = "linux/arm64" ] && echo "aarch64" || echo "x86_64") && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip

# gcloud (Google Cloud SDK) + GKE auth plugin — REQUIRED for the GCP runbooks
# (gcloud alloydb/redis/compute). In a GKE pod, gcloud authenticates via
# Workload Identity (bind the pod's k8s SA to a GCP SA with alloydb/redis/
# compute viewer roles).
RUN echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
      > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
      | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-cloud-cli google-cloud-cli-gke-gcloud-auth-plugin \
    && rm -rf /var/lib/apt/lists/*

# ast-grep (structural code search for code_analyst). ripgrep already installed above.
RUN ARCH=$([ "$TARGETPLATFORM" = "linux/arm64" ] && echo "aarch64" || echo "x86_64") && \
    curl -fsSL "https://github.com/ast-grep/ast-grep/releases/latest/download/app-${ARCH}-unknown-linux-gnu.zip" \
      -o /tmp/sg.zip \
    && unzip -q /tmp/sg.zip -d /usr/local/bin sg ast-grep 2>/dev/null \
    && rm -f /tmp/sg.zip && chmod +x /usr/local/bin/sg /usr/local/bin/ast-grep 2>/dev/null \
    || echo "ast-grep install skipped (code_analyst falls back to ripgrep/git)"

# OpenCode (code_session / fix loop). PINNED to the version code_session was
# built + tested against (the headless server API changes across releases).
# Graceful skip so the build never fails on a transient download.
ARG OPENCODE_VERSION=1.4.3
RUN curl -fsSL https://opencode.ai/install | bash -s -- --version ${OPENCODE_VERSION} \
    && ln -sf /root/.opencode/bin/opencode /usr/local/bin/opencode \
    || echo "opencode install skipped (code_session disabled until present)"

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Copy application
COPY vishwakarma/ ./vishwakarma/
COPY pyproject.toml .

# Console UI bundle (served at /console)
COPY --from=webbuilder /web/dist ./web/dist

# Install the package itself (no deps — already installed above)
RUN pip install --no-cache-dir --no-deps .

# Pre-bake the default local embedding model so pods need no HuggingFace egress
# at runtime (offline-ready, instant start). ~300MB RAM resident, ~2ms/text on
# CPU. Change the model here + embeddings.local_model in config to upgrade.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" || \
    echo "fastembed model pre-bake skipped (RAG will download on first use or run keyword-only)"

# Data directory for SQLite PVC
RUN mkdir -p /data

EXPOSE 5050

CMD ["vk", "serve"]
