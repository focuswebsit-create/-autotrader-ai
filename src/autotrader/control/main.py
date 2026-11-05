"""ASGI entrypoint: ``uvicorn autotrader.control.main:app --port 8001`` (bind to localhost)."""

from autotrader.common.config import get_settings
from autotrader.common.logging import configure_logging
from autotrader.control.app import create_control_app

configure_logging(get_settings().log_level)
app = create_control_app()
