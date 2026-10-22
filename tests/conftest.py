"""Shared test fixtures."""

from pathlib import Path

import custom_components
import pytest

# pytest-homeassistant-custom-component imports its own `custom_components`
# package first; extend it so this repo's integration is importable too.
_REPO_COMPONENTS = str(Path(__file__).parent.parent / "custom_components")
if _REPO_COMPONENTS not in custom_components.__path__:
    custom_components.__path__.append(_REPO_COMPONENTS)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Load custom_components/southern_company; the manifest depends on recorder."""
    yield
