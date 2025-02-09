import sys

from loguru import logger

logger.remove()

logger.add(
    sys.stderr,
    level="INFO",
)

# logger.add(
#     sys.stdout,
#     serialize=True,
#     level="INFO"
# )

logging = logger

# Example usage
if __name__ == "__main__":
    logging.info("This is an info message")
    logging.warning("This is a warning message")
    logging.error("This is an error message")
