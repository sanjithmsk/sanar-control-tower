"""Supply chain control tower."""
import os
from pathlib import Path


def load_env(path=Path(__file__).resolve().parent.parent / ".env"):
    """Local convenience: read KEY=VALUE lines from the git-ignored .env file. Real environment
    variables always win, so a hosting dashboard (Render, etc.) stays in charge in production.
    Set SANAR_DOTENV=0 to skip the file (the tests do)."""
    if os.environ.get("SANAR_DOTENV") == "0" or not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if sep and key and not key.startswith("#") and value and key not in os.environ:
            os.environ[key] = value


load_env()
