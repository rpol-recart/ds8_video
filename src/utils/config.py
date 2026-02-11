"""Application configuration loader."""

import os
import re
import yaml
import logging

logger = logging.getLogger(__name__)

_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)(?::-(.*?))?\}")


def _resolve_env_vars(value):
    """Replace ${VAR:-default} placeholders with environment values."""
    if not isinstance(value, str):
        return value
    def _replacer(match):
        var_name = match.group(1)
        default = match.group(2) if match.group(2) is not None else ""
        return os.environ.get(var_name, default)
    return _ENV_VAR_PATTERN.sub(_replacer, value)


def _walk_and_resolve(obj):
    if isinstance(obj, dict):
        return {k: _walk_and_resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_and_resolve(v) for v in obj]
    return _resolve_env_vars(obj)


def load_config(path: str = "/app/config/app_config.yml") -> dict:
    """Load YAML config and resolve environment variable placeholders."""
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    cfg = _walk_and_resolve(raw)
    logger.info("Configuration loaded from %s", path)
    return cfg
