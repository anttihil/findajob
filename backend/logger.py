import logging
import os
from logging.handlers import RotatingFileHandler

LOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app.log"
)

# Rotation, not a plain FileHandler: under a systemd timer nothing ever truncates this by
# hand, and a scrape logs one line per cell plus every circuit-breaker decision. 5 x 2MB
# keeps roughly the last few weeks of runs -- long enough to explain a bad chart -- at a
# fixed 10MB ceiling.
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

# Create a custom logger
logger = logging.getLogger("job_search")
logger.setLevel(logging.DEBUG)

# Create handlers
c_handler = logging.StreamHandler()
f_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding='utf-8'
)

c_handler.setLevel(logging.INFO)
f_handler.setLevel(logging.DEBUG)

# Create formatters and add them to handlers
log_format = logging.Formatter(
    '[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d]: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
c_handler.setFormatter(log_format)
f_handler.setFormatter(log_format)

# Add handlers to the logger
if not logger.handlers:
    logger.addHandler(c_handler)
    logger.addHandler(f_handler)

def get_logger():
    return logger
