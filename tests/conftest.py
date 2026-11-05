# SPDX-License-Identifier: 0BSD
"""Test tiers and external tools.

PYFLAC_RS_TESTS=full runs the cases marked ``full`` and more Hypothesis
examples; the default ``quick`` tier skips them.

``@pytest.mark.needs_tool("flac")`` skips a test when the program is not on
PATH, or fails it when PYFLAC_RS_REQUIRE_TOOLS=1 (CI sets this where it
installs the tools, so a broken install cannot pass as a skip).
"""

import os
import shutil

import pytest
from hypothesis import HealthCheck, settings

TIER = os.environ.get("PYFLAC_RS_TESTS", "quick")
if TIER not in ("quick", "full"):
    raise pytest.UsageError(f"PYFLAC_RS_TESTS must be quick or full, not {TIER!r}")
REQUIRE_TOOLS = os.environ.get("PYFLAC_RS_REQUIRE_TOOLS") == "1"

settings.register_profile("quick", max_examples=40, deadline=None)
settings.register_profile(
    "full", max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(TIER)


def pytest_report_header(config):
    return f"pyflac-rs test tier: {TIER}, require tools: {REQUIRE_TOOLS}"


def pytest_runtest_setup(item):
    if item.get_closest_marker("full") and TIER != "full":
        pytest.skip("full tier only (PYFLAC_RS_TESTS=full)")
    for mark in item.iter_markers("needs_tool"):
        for tool in mark.args:
            if shutil.which(tool) is None:
                if REQUIRE_TOOLS:
                    pytest.fail(f"{tool} is not on PATH and PYFLAC_RS_REQUIRE_TOOLS=1")
                pytest.skip(f"{tool} not installed")
