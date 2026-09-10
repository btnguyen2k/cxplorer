"""Application logging initialization and namespace defaults."""

import logging

from cxplorer.logging_config import CustomLoggerConfig, configure_logging


def test_custom_logger_reduces_azure_noise_only() -> None:
    assert logging.getLoggerClass() is CustomLoggerConfig
    assert CustomLoggerConfig("azure.identity").level == logging.WARNING
    assert CustomLoggerConfig("azure.core.pipeline", logging.DEBUG).level == logging.WARNING
    assert CustomLoggerConfig("cxplorer.insights", logging.DEBUG).level == logging.DEBUG


def test_configure_logging_uses_the_application_format_before_custom_class(
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        logging,
        "basicConfig",
        lambda **kwargs: calls.append(("basicConfig", kwargs)),
    )
    monkeypatch.setattr(
        logging,
        "setLoggerClass",
        lambda logger_class: calls.append(("setLoggerClass", logger_class)),
    )

    configure_logging()

    assert calls == [
        (
            "basicConfig",
            {
                "level": logging.INFO,
                "format": "%(asctime)s - %(levelname)s - %(name)s: %(message)s",
            },
        ),
        ("setLoggerClass", CustomLoggerConfig),
    ]
