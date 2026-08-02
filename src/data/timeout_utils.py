"""Timeout and fallback utilities for API calls.

Provides:
- Timeout decorators for blocking operations
- Fallback chains (try primary, then backup)
- Parallel execution helpers with per-call timeouts
"""
import concurrent.futures
import functools
import logging
from typing import Any, Callable, Optional, TypeVar
from datetime import datetime

log = logging.getLogger(__name__)

T = TypeVar('T')


class TimeoutError(Exception):
    """Raised when an operation times out."""
    pass


def with_timeout(timeout_sec: float, fallback_value: Any = None):
    """Decorator that adds a timeout to any function call.

    If the function doesn't complete within timeout_sec, returns fallback_value
    instead of hanging forever.

    Usage:
        @with_timeout(timeout_sec=5.0, fallback_value=None)
        def fetch_data():
            return slow_api_call()
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(func, *args, **kwargs)
                    return future.result(timeout=timeout_sec)
            except concurrent.futures.TimeoutError:
                log.warning(f"{func.__name__} timed out after {timeout_sec}s")
                return fallback_value
            except Exception as e:
                log.error(f"{func.__name__} raised {type(e).__name__}: {e}")
                return fallback_value
        return wrapper
    return decorator


def with_fallback(
    primary_func: Callable[..., T],
    fallback_func: Optional[Callable[..., T]] = None,
    timeout_sec: float = 5.0,
    fallback_timeout_sec: float = 5.0,
) -> Callable[..., T]:
    """Returns a function that tries primary_func, then fallback_func on failure.

    Both functions have independent timeouts. If both fail, returns None or
    a sensible default value.

    Usage:
        # Fetch snapshot: try moomoo, fall back to yfinance
        get_snapshot = with_fallback(
            primary_func=moomoo_client.get_stock_snapshot,
            fallback_func=yfinance_client.get_stock_snapshot_fallback,
            timeout_sec=3.0,
            fallback_timeout_sec=5.0,
        )
        result = get_snapshot('US.AAPL')
    """
    def wrapper(*args, **kwargs) -> Optional[T]:
        # Try primary with timeout
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(primary_func, *args, **kwargs)
                return future.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError:
            log.warning(f"Primary {primary_func.__name__} timed out after {timeout_sec}s")
        except Exception as e:
            log.warning(f"Primary {primary_func.__name__} failed: {type(e).__name__}: {e}")

        # Try fallback if available
        if fallback_func is not None:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(fallback_func, *args, **kwargs)
                    return future.result(timeout=fallback_timeout_sec)
            except concurrent.futures.TimeoutError:
                log.warning(f"Fallback {fallback_func.__name__} timed out after {fallback_timeout_sec}s")
            except Exception as e:
                log.warning(f"Fallback {fallback_func.__name__} failed: {type(e).__name__}: {e}")

        return None
    return wrapper


def parallel_map(
    func: Callable[..., T],
    items: list[Any],
    max_workers: int = 5,
    timeout_per_item: float = 10.0,
) -> list[Optional[T]]:
    """Execute func on each item in parallel with per-item timeouts.

    Returns results in the same order as items. Failed/timed-out items return None.

    Usage:
        results = parallel_map(
            func=lambda ticker: fetch_data(ticker),
            items=['AAPL', 'MSFT', 'GOOG'],
            max_workers=5,
            timeout_per_item=10.0,
        )
    """
    results = [None] * len(items)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all jobs
        future_to_index = {
            executor.submit(func, item): idx
            for idx, item in enumerate(items)
        }

        # Wait for completion with timeout
        for future in concurrent.futures.as_completed(future_to_index, timeout=timeout_per_item * len(items) / max_workers + 30):
            idx = future_to_index[future]
            try:
                results[idx] = future.result(timeout=timeout_per_item)
            except concurrent.futures.TimeoutError:
                log.warning(f"Item {idx} ({items[idx]}) timed out after {timeout_per_item}s")
            except Exception as e:
                log.warning(f"Item {idx} ({items[idx]}) failed: {type(e).__name__}: {e}")

    return results


def timed_call(func: Callable[..., T], *args, **kwargs) -> tuple[Optional[T], float]:
    """Execute func and return (result, elapsed_seconds).

    Useful for performance logging. Returns (None, elapsed) on exception.

    Usage:
        result, elapsed = timed_call(client.get_data, 'AAPL')
        log.info(f"Fetched AAPL in {elapsed:.2f}s")
    """
    start = datetime.now()
    try:
        result = func(*args, **kwargs)
        elapsed = (datetime.now() - start).total_seconds()
        return result, elapsed
    except Exception as e:
        elapsed = (datetime.now() - start).total_seconds()
        log.error(f"{func.__name__} failed after {elapsed:.2f}s: {type(e).__name__}: {e}")
        return None, elapsed
