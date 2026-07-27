from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from typing import Callable, TypeVar, cast


ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")
_MISSING = object()


class OrderedBatchExecutor:
    """Run independent work concurrently while preserving source order."""

    def map_ordered(
        self,
        items: list[ItemT],
        worker: Callable[[ItemT], ResultT],
        *,
        max_workers: int,
    ) -> list[ResultT]:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if not items:
            return []
        if max_workers == 1 or len(items) == 1:
            return [worker(item) for item in items]

        results: list[object] = [_MISSING] * len(items)
        with ThreadPoolExecutor(
            max_workers=min(max_workers, len(items)),
            thread_name_prefix="review-batch",
        ) as executor:
            futures = {
                executor.submit(copy_context().run, worker, item): index
                for index, item in enumerate(items)
            }
            try:
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
            except Exception:
                for future in futures:
                    future.cancel()
                raise

        if any(result is _MISSING for result in results):
            raise RuntimeError("batch executor did not produce one result per item")
        return [cast(ResultT, result) for result in results]
