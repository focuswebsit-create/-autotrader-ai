"""ASGI entrypoint: ``uvicorn autotrader.api.main:app``."""

from autotrader.api.app import create_app
from autotrader.common.config import get_settings
from autotrader.common.logging import configure_logging

configure_logging(get_settings().log_level)
app = create_app()
