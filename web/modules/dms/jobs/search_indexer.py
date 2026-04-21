"""
Search Indexer — RQ job wrapper.
Re-exports index_document from search_service for RQ job dispatch.
"""

from modules.dms.services.search_service import index_document
from sqlalchemy import text as sa_text

__all__ = ["index_document"]
