"""测试公共设施：统一日志，打印版本号、游标与判定依据。"""

from __future__ import annotations

import logging
import sys

import pytest

logger = logging.getLogger("vrs.tests")


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger("vrs")
    # 防止重复挂载 handler
    if not any(getattr(h, "_vrs_handler", False) for h in root.handlers):
        handler._vrs_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


_configure_logging()


@pytest.fixture
def log() -> logging.Logger:
    """供各用例记录版本号、游标与判定依据。"""
    return logger
