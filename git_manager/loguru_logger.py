import json
import sys

from loguru import logger


def serialize_log(record):
    log_data = {
        "timestamp": record["time"].timestamp(),
        "level": record["level"].name,
        "message": record["message"],
        "file": record["file"].name,
        "line": record["line"],
        "function": record["function"],
    }
    return json.dumps(log_data)


def log_formatter(record):
    record["extra"]["serialized"] = serialize_log(record)
    return "{extra[serialized]}\n"


def setup_logger():
    logger.remove()

    logger.add(sys.stdout, colorize=True, format=log_formatter, serialize=True)
    return logger


logging = setup_logger()
