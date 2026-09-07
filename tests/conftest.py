import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glasslinkxp.config import default_config  # noqa: E402
from glasslinkxp.ocr import SoftkeyReader  # noqa: E402


@pytest.fixture(scope="session")
def config():
    return default_config()


@pytest.fixture(scope="session")
def reader(config):
    """One Tesseract instance for the whole test session (it is expensive)."""
    reader = SoftkeyReader(config.ocr)
    yield reader
    reader.close()
