from __future__ import annotations

import types

import pytest

from gateway.config import Platform
from gateway.platforms.api_server import APIServerAdapter


@pytest.mark.asyncio
async def test_send_with_retry_short_circuits_on_push_less_adapter(caplog):
    """Regression: no WARN/ERROR ladder from _send_with_retry on api_server.

    BasePlatformAdapter._send_with_retry treats the adapter's contractual
    failure as a formatting error: it logs "[Api_Server] Send failed … —
    trying plain-text fallback" (WARNING), attempts a second send, then logs
    "Fallback send also failed" (ERROR).  On a managed box every mission turn
    delivers its final answer through this path, so errors.log — the cockpit's
    log source — grew one WARNING+ERROR pair per turn (observed live
    2026-08-09: 7 pairs for 5 turns).  The adapter's override must attempt
    send() exactly ONCE, return the contractual failure unchanged, and log
    nothing above DEBUG.  Remove the override and BOTH assertions break
    (two send() calls recorded, WARNING+ERROR captured).
    """
    adapter = APIServerAdapter.__new__(APIServerAdapter)
    adapter.platform = Platform.API_SERVER  # feeds .name, as __init__ would

    calls = []
    real_send = APIServerAdapter.send

    async def counting_send(self, chat_id, content, reply_to=None, metadata=None):
        calls.append(content)
        return await real_send(self, chat_id, content, reply_to=reply_to, metadata=metadata)

    adapter.send = types.MethodType(counting_send, adapter)

    with caplog.at_level("DEBUG"):
        result = await adapter._send_with_retry(
            chat_id="api",
            content="final mission answer",
        )

    assert result.success is False
    assert result.error == "API server uses HTTP request/response, not send()"
    assert calls == ["final mission answer"]
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
