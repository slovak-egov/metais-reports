from pathlib import Path

def load_uuid_list(path: Path) -> set[str]:
    return set(path.read_text("utf-8").splitlines())