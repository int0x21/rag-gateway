"""
Systemd/uvicorn entrypoint.

We keep this module so unit files can reliably reference:
  rag_gateway.main:app

The actual FastAPI app is defined in rag_gateway.api.app.
"""

import uvicorn

from .api.app import app  # noqa: F401


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)


