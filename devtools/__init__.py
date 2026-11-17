"""Development-only tooling that must never be imported by the product runtime.

Everything under :mod:`devtools` is used by the implementation agent while developing
aiagent-md. It is not a product feature and is deliberately excluded from the Docker
images, the Model Gateway, the Agent Runtime, the Sentinel, the Web UI and the public
API (see ``devtools/free_ai/README.md``).
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
