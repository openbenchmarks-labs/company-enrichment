"""Local environment loading shared by live benchmark runners."""
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent


def load_environment() -> None:
    """Load ignored local configuration without overriding shell or CI values."""
    load_dotenv(ROOT / ".env.local")
    load_dotenv(ROOT / ".env")
