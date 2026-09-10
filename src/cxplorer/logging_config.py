"""Application logging defaults installed before CXplorer submodules are imported."""

import logging


class CustomLoggerConfig(logging.Logger):
    def __init__(self, name: str, level: int = logging.NOTSET) -> None:
        super().__init__(name, logging.WARNING if name.startswith("azure.") else level)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s: %(message)s",
    )
    logging.setLoggerClass(CustomLoggerConfig)
