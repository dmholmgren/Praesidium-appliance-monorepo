"""
modules/ediscovery/jobs/guided_propose.py

Sync RQ entrypoint for the guided-ingestion PROPOSE pass. Wraps the async
pipeline.build_proposal so it can run on the (synchronous) RQ worker. Walking a
large client-files tree + the frontier escalation can take a while, so this runs
in the background exactly like run_collection_full.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


def run_proposal(proposal_id, tenant_id, matter_id, source_paths, user_id=None):
    from modules.ediscovery.guided_ingest.pipeline import build_proposal
    logger.info("guided propose job start: proposal=%s paths=%s",
                proposal_id, source_paths)
    return asyncio.run(build_proposal(
        proposal_id, tenant_id, matter_id, source_paths, user_id))
