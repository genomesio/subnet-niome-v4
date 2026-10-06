import logging
import os
import wandb
from logging.handlers import RotatingFileHandler
from niome_subnet.utils.settings import WANDB_MAX_LOGS

logger = logging.getLogger(__name__)

EVENTS_LEVEL_NUM = 38
DEFAULT_LOG_BACKUP_COUNT = 10

def maybe_reset_wandb(validator):
    if validator.config.wandb.off or wandb.run is None:
        return

    log_file_path = os.path.join(wandb.run.dir, "output.log")
    with open(log_file_path, "rb") as f:
        num_lines = sum(1 for _ in f)

    if num_lines > WANDB_MAX_LOGS:
        logger.info("Restarting W&B to limit the number of logs per run!...")
        wandb.run.finish()
        validator.init_wandb()

def setup_events_logger(full_path, events_retention_size):
    logging.addLevelName(EVENTS_LEVEL_NUM, "EVENT")

    logger = logging.getLogger("event")
    logger.setLevel(EVENTS_LEVEL_NUM)

    def event(self, message, *args, **kws):
        if self.isEnabledFor(EVENTS_LEVEL_NUM):
            self._log(EVENTS_LEVEL_NUM, message, args, **kws)

    logging.Logger.event = event

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        os.path.join(full_path, "events.log"),
        maxBytes=events_retention_size,
        backupCount=DEFAULT_LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(EVENTS_LEVEL_NUM)
    logger.addHandler(file_handler)

    return logger
