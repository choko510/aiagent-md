"""Build a disposable SCIP fixture image; never invoked by the runner or Cell.

Network access and package installation occur only while explicitly building the
fixture. The resulting local image ID must be inspected and pinned at test run.
This is not a reproducible, security-attested production toolchain image.
"""

from __future__ import annotations

import base64
import hashlib
import io
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

_URL = "https://registry.npmjs.org/@sourcegraph/scip-python/-/scip-python-0.6.8.tgz"
_SHA512 = "sSQASCJ022nX6tygIxxK4prCNjWBw/fhXkk8kc60BRdS37ECAFtTU3pGXTf7rOb6JCoc+bhB6EtLnrHR3wZfbw=="
_LIMIT = 6_000_000
_TAG = "codex-dev13-scip-cell-fixture"


def main() -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker is required")
    with urllib.request.urlopen(_URL, timeout=30) as response:
        raw = response.read(_LIMIT + 1)
    if len(raw) > _LIMIT or base64.b64encode(hashlib.sha512(raw).digest()).decode("ascii") != _SHA512:
        raise ValueError("SCIP fixture package digest mismatch")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        members = archive.getmembers()
        names = [item.name for item in members]
        if (
            len(members) > 5000
            or len(names) != len(set(names))
            or sum(item.size for item in members) > 30_000_000
            or any(
                not item.name.startswith("package/")
                or ".." in item.name.split("/")
                or not (item.isfile() or item.isdir())
                for item in members
            )
        ):
            raise ValueError("unsafe SCIP fixture archive")
    dockerfile = Path(__file__).with_name("Dockerfile")
    with tempfile.TemporaryDirectory(prefix="dev13-scip-build-") as directory:
        context = Path(directory)
        (context / "scip.tgz").write_bytes(raw)
        shutil.copyfile(dockerfile, context / "Dockerfile")
        subprocess.run([docker, "build", "--tag", _TAG, str(context)], check=True, timeout=900)  # noqa: S603
    subprocess.run([docker, "image", "inspect", _TAG, "--format", "{{.Id}}"], check=True, timeout=30)  # noqa: S603


if __name__ == "__main__":
    main()
