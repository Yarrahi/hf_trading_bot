import logging

def setup_logger(name=None):
    log_level = logging.INFO
    logger = logging.getLogger(name if name else "trading_bot")
    logger.setLevel(log_level)

    # Prevent duplicate logs
    if logger.hasHandlers():
        logger.handlers.clear()

    handler = logging.StreamHandler()
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False

    return logger