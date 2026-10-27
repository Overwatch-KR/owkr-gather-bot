"""순서를 지키는 저장 큐의 계약. 큐 접수와 DB 커밋 완료를 구분한다."""

from typing import Protocol

from src.domain.models import PersistenceMutation


class MutationWriter(Protocol):
    def submit(self, mutation: PersistenceMutation) -> None:
        """변경을 순서대로 접수한다. 반환 시점에 DB 기록을 보장하지 않는다."""
        ...

    async def flush(self) -> None:
        """이 호출보다 먼저 접수된 변경의 DB 기록 완료까지 기다린다."""
        ...


class ManagedMutationWriter(MutationWriter, Protocol):
    def start(self) -> None: ...

    async def stop(self) -> None:
        """남은 변경을 기록한 후 저장 작업을 종료한다."""
        ...
