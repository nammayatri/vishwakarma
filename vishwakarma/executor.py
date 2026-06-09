"""
Executor — per-cloud investigation worker.

Consumes cloud-tagged jobs from the Redis job stream and runs the SAME
investigation flow as the all-in-one server (`_do_investigation`: Slack ack,
fast-RCA, agentic loop with step checkpointing, PDF, Slack post, DB save,
dedup release). Deployed inside the cloud it serves (EKS for aws, GKE for
gcp) so its toolsets can reach that cloud's VPC-internal data plane.

Crash safety (at-least-once, idempotent):
  - the stream message stays pending until XACK after the investigation
    finishes (success OR handled failure);
  - a periodic reaper XAUTOCLAIMs jobs whose executor died;
  - the investigations table (claim/attempt budget/checkpoints) makes
    re-delivery resume instead of duplicate.

Run:  vk serve-executor --cloud aws
"""
import asyncio
import logging
import signal
import socket
import threading
import time

log = logging.getLogger(__name__)

REAP_INTERVAL = 60          # seconds between stale-job sweeps
CONSUME_BLOCK_MS = 5_000


class Executor:
    def __init__(self, config, cloud: str):
        if cloud not in ("aws", "gcp"):
            raise ValueError(f"cloud must be aws|gcp, got {cloud!r}")
        self.config = config
        self.cloud = cloud
        self.consumer = f"{cloud}-{socket.gethostname()}"
        self._stop = threading.Event()
        self._state: dict = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        cfg = self.config
        from vishwakarma.storage.db import init_db
        init_db(cfg.db_path, dsn=cfg.pg_dsn)
        from vishwakarma.storage import dedup
        dedup.init_dedup(cfg.redis_url)
        from vishwakarma.core.embeddings import init_embeddings
        init_embeddings(cfg.embeddings_api_base, cfg.embeddings_api_key,
                        cfg.embeddings_model, cfg.embeddings_dim)
        from vishwakarma.core.jobstream import init_jobstream
        if not cfg.redis_url:
            raise RuntimeError("executor requires storage.redis_url (job stream)")
        init_jobstream(cfg.redis_url)
        from vishwakarma.core.eventbus import init_eventbus
        init_eventbus(cfg.redis_url)  # publish live events to the console UI
        from vishwakarma.core.keypool import init_keypool
        init_keypool(cfg.llm.api_keys or ([cfg.llm.api_key] if cfg.llm.api_key else []))
        try:
            from vishwakarma.storage.runbooks import seed_from_files
            seed_from_files()
        except Exception as e:
            log.warning(f"Runbook seeding failed: {e}")

        from vishwakarma.core.learnings import LearningsManager
        self._state["learnings"] = LearningsManager()
        self._state["toolset_manager"] = cfg.make_toolset_manager()
        self._state["toolset_manager"].check_all()

        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())

        log.info(f"Executor ready: cloud={self.cloud} consumer={self.consumer}")
        self._loop()

    def stop(self) -> None:
        log.info("Executor stopping (finishing current job)...")
        self._stop.set()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        from vishwakarma.core import jobstream
        last_reap = 0.0
        while not self._stop.is_set():
            # Reap jobs from dead executors periodically
            if time.time() - last_reap > REAP_INTERVAL:
                last_reap = time.time()
                try:
                    for msg_id, payload in jobstream.claim_stale(self.cloud, self.consumer):
                        self._run_job(msg_id, payload, reclaimed=True)
                except Exception as e:
                    log.warning(f"Stale-job reap failed: {e}")

            try:
                got = jobstream.consume(self.cloud, self.consumer, block_ms=CONSUME_BLOCK_MS)
            except Exception as e:
                log.error(f"Job consume failed: {e} — retrying in 5s")
                time.sleep(5)
                continue
            if got is None:
                continue
            msg_id, payload = got
            self._run_job(msg_id, payload)

    # ── Job execution ─────────────────────────────────────────────────────────

    def _run_job(self, msg_id: str, payload: dict, reclaimed: bool = False) -> None:
        from vishwakarma.core import jobstream
        from vishwakarma.core.issue import Issue
        from vishwakarma.storage.investigations import get_investigation

        base_incident_id = payload.get("incident_id", "")
        fingerprint = payload.get("fingerprint", "")
        # `both`-cloud jobs are fanned to both pools with the same payload. Each
        # half tracks under a cloud-suffixed id (so the two halves don't collide
        # in the investigations table) but writes findings under the base id.
        is_both = payload.get("cloud", "") == "both"
        cross_cloud = self.cloud if is_both else ""
        incident_id = f"{base_incident_id}:{self.cloud}" if is_both else base_incident_id
        try:
            issue = Issue.model_validate(payload["issue"])
        except Exception as e:
            log.error(f"Job {msg_id}: bad payload ({e}) — acking to drop")
            jobstream.ack(self.cloud, msg_id)
            return

        # Idempotent re-delivery: if it's already terminal, just ack.
        try:
            existing = get_investigation(incident_id)
            if existing and existing.get("status") in ("done", "failed"):
                log.info(f"Job {msg_id}: investigation {incident_id} already "
                         f"{existing['status']} — acking duplicate delivery")
                jobstream.ack(self.cloud, msg_id)
                return
        except Exception:
            pass

        tag = " (reclaimed)" if reclaimed else ""
        log.info(f"Executor {self.consumer}: investigating '{issue.title[:60]}'"
                 f" [{incident_id}]{tag}")
        try:
            # Reuse the full all-in-one investigation flow — ack/fast-RCA/
            # agentic loop (with checkpointing)/PDF/Slack/save/dedup-release.
            from vishwakarma.server import _do_investigation
            asyncio.run(_do_investigation(
                self.config, self._state, issue, incident_id, fingerprint,
                cross_cloud=cross_cloud, cross_cloud_base=base_incident_id))
        except Exception as e:
            log.error(f"Job {msg_id} investigation crashed: {e}", exc_info=True)
            # Leave the dedup lock to its TTL; mark investigation failed so the
            # attempt budget governs any re-delivery.
            try:
                from vishwakarma.storage.investigations import finish_investigation
                finish_investigation(incident_id, "failed")
            except Exception:
                pass
        finally:
            # Ack in all handled cases — re-delivery for *crash-before-ack* is
            # what XAUTOCLAIM covers; in-process failures are terminal above.
            try:
                jobstream.ack(self.cloud, msg_id)
            except Exception as e:
                log.warning(f"Ack failed for {msg_id}: {e}")


def run_executor(config, cloud: str) -> None:
    Executor(config, cloud).start()
