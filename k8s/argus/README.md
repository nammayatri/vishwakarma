# Argus multi-pod topology — k8s manifests

Plain YAML, applied with `kubectl` (no Helm). The topology:

```
orchestrator (GCP, 1 replica)  ── webhook + Slack bots + console UI
        │  Redis Streams (Memorystore)        + cloud routing + enqueue
        ├── executor-gcp (GKE, N replicas)    investigates GCP-side alerts
        └── executor-aws (EKS, N replicas)    investigates AWS-side alerts
control plane: Cloud SQL Postgres (pgvector) + Memorystore Redis — both in
GCP, reached from AWS executors over the existing VPC peering.
```

## Apply order

```bash
# 0. Create the namespace + secrets first (copy the examples, fill in real
#    values — NEVER commit filled-in secrets)
kubectl apply -f namespace.yaml
cp secrets.example.yaml /tmp/secrets.yaml   # edit, then:
kubectl apply -f /tmp/secrets.yaml && rm /tmp/secrets.yaml

# 1. Config
cp configmap.example.yaml configmap.yaml    # edit endpoints for your site
kubectl apply -f configmap.yaml

# 2. Orchestrator (GCP cluster)
kubectl --context=<gke-context> apply -f orchestrator.yaml

# 3. Executors (each in its own cloud's cluster)
kubectl --context=<gke-context> apply -f executor-gcp.yaml
kubectl --context=<eks-context> apply -f executor-aws.yaml
```

## Shadow mode

Run this topology alongside the existing single-pod deployment: point a COPY
of the AlertManager webhook at the orchestrator and let results post to a
test Slack channel (set SLACK_CHANNEL in the orchestrator env). Flip the real
webhook + channel once parity is proven.

## Notes

- The single-pod all-in-one mode (`vk serve`) needs none of this — it's the
  default for local/OSS quickstart.
- Executors mount a PVC at /data/repos for the code-analyst repo cache.
- Per-cloud knowledge: mount knowledge-aws.md / knowledge-gcp.md on the
  respective executors' /data.
