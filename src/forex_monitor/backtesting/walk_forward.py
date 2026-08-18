"""Leak-free expanding and rolling walk-forward windows."""

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class WalkForwardWindow:
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def rolling_windows(
    length: int, train_size: int, test_size: int, step: int = 0
) -> Tuple[WalkForwardWindow, ...]:
    if min(length, train_size, test_size) < 1:
        raise ValueError("walk-forward sizes must be positive")
    stride = step or test_size
    if stride < 1:
        raise ValueError("walk-forward step must be positive")
    result = []
    train_start = 0
    while train_start + train_size + test_size <= length:
        train_end = train_start + train_size
        result.append(WalkForwardWindow(train_start, train_end, train_end, train_end + test_size))
        train_start += stride
    return tuple(result)


def expanding_windows(
    length: int, initial_train_size: int, test_size: int
) -> Tuple[WalkForwardWindow, ...]:
    rolling = rolling_windows(length, initial_train_size, test_size)
    return tuple(
        WalkForwardWindow(0, window.train_end, window.test_start, window.test_end)
        for window in rolling
    )
