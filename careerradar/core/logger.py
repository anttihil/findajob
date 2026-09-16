import logging

from careerradar.core.paths import LOG_PATH

LOG_FILE = LOG_PATH

# A plain FileHandler, not RotatingFileHandler: four units -- three timers plus the
# long-lived web server -- hold this file open at once, and RotatingFileHandler assumes a
# single writer. When one process rotated, the others kept appending to the renamed inode,
# whose next rotation deletes it. The evidence is in this checkout: app.log.3 is 74 bytes
# over the old 2MB cap, which shouldRollover() makes impossible for one writer, and its last
# line is 71 seconds *newer* than app.log.2's.
#
# Rotation is logrotate's job now, with `copytruncate` so every writer keeps the same inode
# across a rotation. See deploy/install-systemd.sh -- without it installed, this file
# grows without bound.
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
