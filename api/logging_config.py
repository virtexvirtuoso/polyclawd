"""
Polyclawd logging configuration using loguru.
Intercepts stdlib logging and provides colored terminal output with rotation.
"""
import logging
import sys
from pathlib import Path
from loguru import logger


class InterceptHandler(logging.Handler):
    """Intercept stdlib logging and route through loguru."""
    
    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where the logged message originated
        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def suppress_noisy_routes(record):
    """Filter out noisy health check and polling routes at INFO level."""
    message = record["message"]
    
    # Noisy routes that fire every 30-40s
    noisy_patterns = [
        "/api/hf/signal/",
        "/health",
        "/ready",
        "/api/signals/ai-models"
    ]
    
    # If it's an INFO-level uvicorn access log with a noisy pattern, suppress it
    if record["level"].name == "INFO":
        for pattern in noisy_patterns:
            if pattern in message:
                return False
    
    return True


def setup_logging(log_level: str = "INFO"):
    """
    Setup loguru logging with:
    - Colored terminal output
    - Log file rotation (10MB, 7 days retention)
    - Stdlib logging interception
    - Noisy route suppression
    """
    # Remove default logger
    logger.remove()
    
    # Add colored terminal output
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> | <level>{message}</level>",
        level=log_level,
        colorize=True,
        filter=suppress_noisy_routes
    )
    
    # Add file output with rotation
    log_path = Path(__file__).parent.parent / "logs" / "polyclawd.log"
    logger.add(
        log_path,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name} | {message}",
        level=log_level,
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        filter=suppress_noisy_routes
    )
    
    # Intercept stdlib logging
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    
    # Intercept specific loggers
    for logger_name in ["uvicorn", "uvicorn.error", "fastapi"]:
        logging_logger = logging.getLogger(logger_name)
        logging_logger.handlers = [InterceptHandler()]
        logging_logger.propagate = False
    
    # Disable uvicorn access logger entirely — it's just noise
    # Important events are captured via activity_feed + middleware
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False
    access_logger.disabled = True
    
    logger.info("Logging system initialized with loguru")
