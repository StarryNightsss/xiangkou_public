"""Stable error codes for Agent control; never expose raw upstream messages."""
from dataclasses import dataclass, asdict

import httpx

from .dianping import DianpingError, DianpingRejected, DianpingUnauthorized
from .run_context import BudgetExceeded, HarnessError


@dataclass(frozen=True)
class AgentError:
    code: str
    retryable: bool = False
    terminal: bool = False
    stage: str = "tool"

    def to_wire(self):
        return asdict(self)


def classify_error(exc: Exception) -> AgentError:
    if isinstance(exc, BudgetExceeded):
        return AgentError(exc.code, terminal=True)
    if isinstance(exc, HarnessError):
        return AgentError(exc.code)
    if isinstance(exc, DianpingUnauthorized):
        return AgentError('auth_expired', terminal=True)
    if isinstance(exc, DianpingRejected):
        return AgentError('business_rejected')
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return AgentError('auth_expired', terminal=True)
        # A rate limit needs Retry-After handling, not immediate retry.
        if status == 429:
            return AgentError('rate_limited', terminal=True)
        return AgentError('upstream_unavailable' if status >= 500 else 'upstream_rejected',
                          retryable=status >= 500)
    if isinstance(exc, DianpingError) and isinstance(exc.__cause__, httpx.HTTPStatusError):
        return classify_error(exc.__cause__)
    if isinstance(exc, DianpingError) and isinstance(exc.__cause__, ValueError):
        return AgentError('upstream_protocol_error')
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return AgentError('tool_timeout', retryable=True)
    if isinstance(exc, (DianpingError, httpx.TransportError)):
        return AgentError('upstream_unavailable', retryable=True)
    return AgentError('internal_error', terminal=True)
