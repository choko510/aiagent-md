"""Safe multimodal ingestion (15-01).

The package keeps four boundaries explicit:

* a raw byte is **untrusted input** and never becomes a model instruction;
* a parser result is a **candidate**, not verified evidence;
* a locator binds a candidate to the immutable source artifact and SHA-256;
* production parsing requires a fail-closed isolation attestation (never a
  silent fallback inside the trusted runtime).

Only the standard library is required. Optional adapters (pdfplumber, Pillow,
Tesseract) are imported lazily so a missing engine degrades to an explicit
warning instead of changing the meaning of the result.
"""

from .contracts import (
    MmiCandidate,
    MmiOutcomeV1,
    MmiRequestV1,
    MmiSource,
    MmiSourceLocator,
    canonical_parse_key,
)
from .errors import MmiError
from .limits import MmiLimits

__all__ = [
    "MmiCandidate",
    "MmiError",
    "MmiLimits",
    "MmiOutcomeV1",
    "MmiRequestV1",
    "MmiSource",
    "MmiSourceLocator",
    "canonical_parse_key",
]
