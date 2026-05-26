# tests/conftest.py
# Patch load_dotenv for the entire test session so tests never depend on a
# real .env file and don't accidentally pick up developer credentials.

from unittest.mock import patch
import pytest


@pytest.fixture(autouse=True, scope="session")
def no_dotenv():
    with patch("gmail_analyzer.load_dotenv"):
        yield
