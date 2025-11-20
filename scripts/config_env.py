import os
import re
from pathlib import Path

#flags for VALID_FLAG
VALID_BOTH     = 0b11   # 3
VALID_ONLY     = 0b01   # 1
INVALID_ONLY   = 0b10   # 2

SCRIPT_DIR = Path(__file__).resolve().parent

def parse_valid_flag(value: str, default: str = "both") -> int:
    """
    Parse a VALID_FLAG-like string into a bitmask.

    "both", "", "all", "*" -> VALID_BOTH
    "valid", "true", "1", "yes" -> VALID_ONLY
    "invalid", "false", "0", "no" -> INVALID_ONLY
    """
    if value is None:
        value = default
    v = value.strip().lower()
    if v in ("both", "", "all", "*"):
        return VALID_BOTH
    if v in ("valid", "true", "1", "yes"):
        return VALID_ONLY
    if v in ("invalid", "false", "0", "no"):
        return INVALID_ONLY
    # fallback
    return VALID_BOTH


def get_valid_flag(env_var: str = "VALID_FLAG", default: str = "both") -> int:
    """
    Read VALID_FLAG from env and return parsed bitmask.
    """
    raw = os.getenv(env_var, default)
    return parse_valid_flag(raw, default=default)


def parse_include_types(raw: str) -> set[str]:
    """
    Parse a comma-separated INCLUDE_TYPES string into a lowercase set.
    e.g. "application, system, codelist" -> {"application","system","codelist"}
    """
    return {
        s.strip().lower()
        for s in (raw or "").split(",")
        if s.strip()
    }


def get_include_types(
    env_var: str = "INCLUDE_TYPES",
    default: str = "application,system,codelist",
) -> set[str]:
    """
    Read INCLUDE_TYPES from env and parse into a lowercase set.
    """
    raw = os.getenv(env_var, default)
    return parse_include_types(raw)

def load_env_file(path=f"{SCRIPT_DIR}/.metais.env"):
    if not os.path.exists(path):
        return

    var_ref = re.compile(r"\$(\w+)")  # matches $VAR

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()

            # expand references to previously defined env vars
            def repl(match):
                ref = match.group(1)
                return os.environ.get(ref, "")

            val = var_ref.sub(repl, val)

            os.environ[key] = val
