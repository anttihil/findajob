import logging

from findajob.core.paths import LOG_PATH

LOG_FILE = LOG_PATH

# Use a plain FileHandler because multiple processes write concurrently; logrotate owns
# rotation and must use copytruncate to preserve the inode held by each writer.
logger = logging.getLogger("job_search")
logger.setLevel(logging.DEBUG)

c_handler = logging.StreamHandler()
f_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")

c_handler.setLevel(logging.INFO)
f_handler.setLevel(logging.DEBUG)

log_format = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
c_handler.setFormatter(log_format)
f_handler.setFormatter(log_format)

logging.getLogger("uvicorn.error").addHandler(f_handler)

if not logger.handlers:
    logger.addHandler(c_handler)
    logger.addHandler(f_handler)


def get_logger():
    return logger
