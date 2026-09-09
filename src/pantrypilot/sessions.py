"""Session manager factory.

Locally, Strands ``FileSessionManager`` persists under ``.data/sessions``. On AgentCore the
filesystem is ephemeral, so when ``SESSION_BUCKET`` is set we use ``S3SessionManager``.
"""

from __future__ import annotations

import os

from strands.session import FileSessionManager, S3SessionManager
from strands.session.session_manager import SessionManager

from . import config


def build_session_manager(session_id: str) -> SessionManager:
    """Return a session manager for ``session_id`` (file-backed locally, S3 when configured)."""
    bucket = os.getenv("SESSION_BUCKET")
    if bucket:
        return S3SessionManager(
            session_id=session_id,
            bucket=bucket,
            prefix=os.getenv("SESSION_PREFIX", "pantrypilot/sessions"),
            region_name=os.getenv("AWS_REGION", "us-west-2"),
        )
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return FileSessionManager(session_id=session_id, storage_dir=str(config.SESSIONS_DIR))
