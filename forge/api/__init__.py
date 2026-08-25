"""HTTP service surface.

Importing this package requires the `api` extra:

    pip install "forge-runtime[api]"
"""

from __future__ import annotations

from forge.api.app import create_app

__all__ = ["create_app"]
