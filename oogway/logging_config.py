"""Centralized logging configuration for Oogway bot."""

import logging
import sys
from typing import Optional


def setup_logging(level: Optional[str] = None) -> None:
    """
    Configure logging for the entire application.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
               Defaults to INFO if not specified.
    """
    log_level = getattr(logging, level.upper()) if level else logging.INFO

    # Root logger configuration
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )

    # Specific logger configurations
    # Suppress overly verbose loggers
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Set application loggers to appropriate levels
    logging.getLogger("oogway").setLevel(log_level)
    logging.getLogger("oogway.bot").setLevel(logging.INFO)
    logging.getLogger("oogway.riot").setLevel(logging.INFO)
    logging.getLogger("oogway.cogs").setLevel(logging.INFO)

    # Log the setup completion
    logger = logging.getLogger("oogway.logging_config")
    logger.info(f"Logging initialized at level: {logging.getLevelName(log_level)}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with consistent configuration.

    Args:
        name: The logger name (typically __name__)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)
