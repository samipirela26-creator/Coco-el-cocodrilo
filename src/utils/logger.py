"""Configuración de logging. Adaptado de telegram-bot-gastos-llm."""
import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logger(config) -> logging.Logger:
    logger = logging.getLogger('gastos-bot')
    logger.setLevel(getattr(logging, config.log_level, logging.INFO))

    os.makedirs(config.log_dir, exist_ok=True)

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        os.path.join(config.log_dir, 'gastos-bot.log'),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
