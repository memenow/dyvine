import logging
import sys
from pathlib import Path

def setup_logging():
    """Configure logging"""
    log_path = Path("logs")
    log_path.mkdir(exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path / "api.log")
        ]
    )
