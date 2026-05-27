"""Tests for mailmind.config.Settings."""

from __future__ import annotations

from unittest.mock import patch

from mailmind.config import Settings


class TestSettingsDefaults:
    def test_default_rag_db_path(self) -> None:
        assert Settings().rag_db_path == "mailmind.db"

    def test_default_scheduler_enabled(self) -> None:
        assert Settings().scheduler_enabled is True

    def test_default_ingest_interval(self) -> None:
        assert Settings().ingest_interval_minutes == 360

    def test_default_service_port(self) -> None:
        assert Settings().service_port == 8765


class TestSettingsFromEnv:
    def test_reads_rag_db_path(self) -> None:
        with patch.dict("os.environ", {"RAG_DB_PATH": "/tmp/test.db"}):
            assert Settings.from_env().rag_db_path == "/tmp/test.db"

    def test_reads_ollama_model(self) -> None:
        with patch.dict("os.environ", {"OLLAMA_MODEL": "llama3:8b"}):
            assert Settings.from_env().ollama_model == "llama3:8b"

    def test_reads_ingest_cron(self) -> None:
        with patch.dict("os.environ", {"INGEST_CRON": "0 2 * * *"}):
            assert Settings.from_env().ingest_cron == "0 2 * * *"

    def test_scheduler_disabled_by_false(self) -> None:
        with patch.dict("os.environ", {"SCHEDULER_ENABLED": "false"}):
            assert Settings.from_env().scheduler_enabled is False

    def test_scheduler_disabled_by_0(self) -> None:
        with patch.dict("os.environ", {"SCHEDULER_ENABLED": "0"}):
            assert Settings.from_env().scheduler_enabled is False

    def test_int_conversion(self) -> None:
        with patch.dict("os.environ", {"GMAIL_MAX_RESULTS": "50"}):
            assert Settings.from_env().gmail_max_results == 50

    def test_float_conversion(self) -> None:
        with patch.dict("os.environ", {"OLLAMA_TIMEOUT": "60.5"}):
            assert Settings.from_env().ollama_timeout == 60.5

    def test_missing_var_uses_default(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            cfg = Settings.from_env()
            assert cfg.service_host == "127.0.0.1"
