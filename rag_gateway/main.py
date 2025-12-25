"""
Systemd/uvicorn entrypoint.

We keep this module so unit files can reliably reference:
  rag_gateway.main:app

The actual FastAPI app is defined in rag_gateway.app.
"""

from .app import app  # noqa: F401

