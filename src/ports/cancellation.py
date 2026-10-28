from typing import Protocol

from src.domain.models import CancellationJob


class CancellationTransport(Protocol):
    async def cleanup(self, job: CancellationJob) -> bool: ...

    async def notify(self, job: CancellationJob) -> bool: ...
