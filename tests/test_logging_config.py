"""Application logging initialization and namespace defaults."""

import logging

from cxplorer.logging_config import CustomLoggerConfig


def test_custom_logger_reduces_azure_noise_only() -> None:
    assert logging.getLoggerClass() is CustomLoggerConfig
    assert CustomLoggerConfig("azure.identity").level == logging.WARNING
    assert CustomLoggerConfig("azure.core.pipeline", logging.DEBUG).level == logging.WARNING
    assert CustomLoggerConfig("cxplorer.insights", logging.DEBUG).level == logging.DEBUG
