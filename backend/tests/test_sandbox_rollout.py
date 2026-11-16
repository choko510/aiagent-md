"""Grant wire compatibility against the pre-attempt main implementation.

The legacy fixture is copied from main ac8185c4b9ceb7a1c7b52e296e50331e4d783065.
This checks signed grant parsing, not a mixed-version deployment or network isolation.
"""

from __future__ import annotations

import legacy_sandbox_grant_fixture as old
import pytest
from aiagent_sandbox import grant as current


@pytest.mark.parametrize(
    ("gateway", "supervisor", "accepted"),
    [
        ("old", "old", True),
        ("old", "new", False),
        ("new", "old", False),
        ("new", "new", True),
    ],
)
def test_grant_version_rollout_matrix(gateway: str, supervisor: str, accepted: bool) -> None:
    key = b"k" * 32
    args = ("g", "tenant", "task", "run", "intent", 1, ("command",), ".", 1000, 1024, 9999999999)
    if gateway == "old":
        token = old.sign(key, old.Grant(*args))
    else:
        token = current.sign(key, current.Grant(*args, attempt_id="attempt", attempt_binding_version=1))
    module = old if supervisor == "old" else current
    if accepted:
        result = module.verify(key, token, ["command"], ".")
        assert result.tenant_id == "tenant" and result.lease_epoch == 1
    else:
        with pytest.raises(module.GrantError, match="MALFORMED|LEGACY_DISABLED"):
            module.verify(key, token, ["command"], ".")


def test_legacy_opt_in_is_explicit_and_not_default() -> None:
    key = b"k" * 32
    args = ("g", "tenant", "task", "run", "intent", 1, ("command",), ".", 1000, 1024, 9999999999)
    token = old.sign(key, old.Grant(*args))
    with pytest.raises(current.GrantError, match="LEGACY_DISABLED"):
        current.verify(key, token, ["command"], ".")
    assert current.verify(key, token, ["command"], ".", allow_legacy=True).attempt_binding_version is None
