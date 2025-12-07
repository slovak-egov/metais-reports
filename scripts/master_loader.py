#!/usr/bin/env python3
import sys, os
from datetime import datetime
from pathlib import Path
import importlib.util
import json
from typing import Any
import argparse

from tqdm import tqdm

from json_writer import dump_json_smart
from config_env import find_project_root, load_env_file

# your packed reader + repacker
from packed_reader import PackedStore   # <- adjust module name if different

load_env_file()

PRETTY_JSON = os.getenv("META_VIZ_PRETTY_JSON", "1").strip().lower() in ("1", "true", "yes", "y")
JSON_INDENT = 2 if PRETTY_JSON else None
JSON_SEPARATORS = (",", ": ") if PRETTY_JSON else (",", ":")

# ---------- helpers ----------

def check_date(date_str: str) -> None:
    try:
        datetime.strptime(date_str, "%d-%m-%Y")
    except ValueError:
        print(f"Invalid date '{date_str}': expected format dd-mm-yyyy and a real calendar date")
        sys.exit(1)


def parse_loadout_file(path: Path) -> dict[str, set[str]]:
    """
    Very simple parser:

    dataset
        metais_dup
        attributes_stats
    graph
        graph_overview

    Returns:
      {
        "dataset": {"metais_dup", "attributes_stats"},
        "graph": {"graph_overview"},
      }
    """
    mapping: dict[str, set[str]] = {}
    if not path.is_file():
        print(f"[loadout] WARNING: loadout file {path} not found, running all modules")
        return mapping

    current_category: str | None = None

    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip():
                continue  # blank
            if line.lstrip().startswith("#"):
                continue  # comment

            if line[0].isspace():
                # module name under current category
                if current_category is None:
                    continue
                mod_name = line.strip()
                if not mod_name:
                    continue
                mapping.setdefault(current_category, set()).add(mod_name)
            else:
                # new category
                current_category = line.strip()
                if current_category not in mapping:
                    mapping[current_category] = set()

    return mapping


class AttributeLabeler:
    """
    Shared attribute-label dictionary with optional interactive prompting.

    - labels: mapping technicalName -> human-readable label
    - interactive: whether to ask user for missing labels
    - path: where to persist the mapping as JSON
    """

    def __init__(self, labels: dict[str, str], interactive: bool, path: Path):
        self.labels = labels
        self.interactive = interactive
        self.path = path
        self.dirty = False

    def get_label(
        self,
        technical_name: str,
        metadata_label: str | None = None,
    ) -> str:
        """
        Resolution order:

          1) If metadata_label is provided -> always use that (ground truth)
          2) Else, if we have a stored custom label -> use that
          3) Else, if interactive + TTY -> prompt user and store
          4) Else, fall back to technical_name
        """

        # 1) metadata is the source of truth – never override it
        if metadata_label:
            return metadata_label

        # 2) user-defined label (for attributes *without* metadata)
        if technical_name in self.labels:
            return self.labels[technical_name]

        # 3) prompt if interactive
        fallback = technical_name
        if self.interactive and sys.stdin.isatty():
            print(
                f'\n[attr-labels] Human-readable "name" not found for {technical_name}.'
            )
            print(
                '  Enter a suitable label (e.g. "Metóda riadenia projektu")\n'
                "  or press Enter to keep the technical name."
            )
            user_input = input("  -> ").strip()
            if user_input:
                self.labels[technical_name] = user_input
                self.dirty = True
                return user_input

        # 4) final fallback
        return fallback

    def save_if_dirty(self):
        if not self.dirty:
            return
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(self.labels, f, ensure_ascii=False, indent=2)
            print(f"[attr-labels] Updated {self.path}")
            self.dirty = False
        except Exception as e:
            print(f"[attr-labels] WARNING: failed to write {self.path}: {e}")


# ---------- paths that don't depend on args ----------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = find_project_root(SCRIPT_DIR)
MODULES_DIR_NAME = os.getenv("MODULES_DIR_NAME", "modules")
MODULES_DIR      = SCRIPT_DIR / MODULES_DIR_NAME

OUTPUT_DIR_NAME = os.getenv("METAIS_RAW_OUTPUT_ROOT", "output")

env_path = os.getenv("META_VIZ_DATA_ROOT")
if env_path:
    METAVIZ_OUTPUT_ROOT = (PROJECT_ROOT / env_path).resolve()
else:
    METAVIZ_OUTPUT_ROOT = PROJECT_ROOT / "meta-viz" / "data"

INTERACTIVE_ATTRIBUTES = os.getenv("INTERACTIVE_ATTRIBUTES", "false").strip().lower() in (
    "1", "true", "yes", "y"
)
ATTR_LABELS_PATH = PROJECT_ROOT / "params" / "attribute_labels.json"
ATTR_LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
try:
    if ATTR_LABELS_PATH.is_file():
        with ATTR_LABELS_PATH.open("r", encoding="utf-8") as f:
            attribute_labels: dict[str, str] = json.load(f)
    else:
        attribute_labels = {}
except Exception as e:
    print(f"[attr-labels] WARNING: failed to load {ATTR_LABELS_PATH}: {e}")
    attribute_labels = {}

attr_labeler = AttributeLabeler(
    labels=attribute_labels,
    interactive=INTERACTIVE_ATTRIBUTES,
    path=ATTR_LABELS_PATH,
)

# ---------- CLI args ----------

parser = argparse.ArgumentParser(
    description="MetaIS snapshot processor + meta-viz data generator (packed-reader only)"
)
parser.add_argument(
    "date",
    help="Snapshot date in format dd-mm-yyyy",
)
parser.add_argument(
    "-l", "--loadout",
    dest="loadout",
    help="Optional path to loadout file; if omitted, uses env META_VIZ_LOADOUT; if neither set, runs all modules",
    default=None,
)

cli_args = parser.parse_args()

DATE = cli_args.date
check_date(DATE)

# determine loadout path (if any)
loadout_path: Path | None = None
if cli_args.loadout:
    loadout_path = Path(cli_args.loadout)
else:
    env_loadout = os.getenv("META_VIZ_LOADOUT")
    if env_loadout:
        loadout_path = (SCRIPT_DIR / env_loadout).resolve()

if loadout_path is not None:
    LOADOUT = parse_loadout_file(loadout_path)
else:
    LOADOUT = {}

# ---------- date-dependent paths ----------

METAVIZ_OUTPUT  = METAVIZ_OUTPUT_ROOT / DATE
DATA_DIR_ROOT   = PROJECT_ROOT / OUTPUT_DIR_NAME / DATE
PACKED_DIR      = DATA_DIR_ROOT / "packed"

METADATA_ROOT   = DATA_DIR_ROOT / "metadata"
ENUMS_MERGED_PATH = METADATA_ROOT / "enums_merged.json"

if not DATA_DIR_ROOT.is_dir():
    print(f"[error] Data root {DATA_DIR_ROOT} does not exist. Aborting.")
    sys.exit(1)

# ---------- enums ----------

def load_json_or_gz(path: Path) -> Any:
    """
    Just a simple JSON loader for small metadata / enums files.
    If you still may gzip these, you can extend this to support .gz.
    """
    if path.suffix == ".gz":
        import gzip
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    else:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)


try:
    enums_merged_raw = load_json_or_gz(ENUMS_MERGED_PATH)
    if isinstance(enums_merged_raw, dict):
        enums_merged = enums_merged_raw
    else:
        print(f"[enums] WARNING: {ENUMS_MERGED_PATH} is not a dict, ignoring")
        enums_merged = {}
except FileNotFoundError:
    print(f"[enums] WARNING: {ENUMS_MERGED_PATH} not found, enums_merged will be empty")
    enums_merged = {}
except Exception as e:
    print(f"[enums] WARNING: failed to load {ENUMS_MERGED_PATH}: {e}")
    enums_merged = {}


# ---------- Context over PACKED store ----------

class Context:
    """
    Thin façade over the PackedStore + metadata + labelling.
    Modules should use:

        ctx.store         -> PackedStore instance (low-level access)
        ctx.enums         -> enum dictionary
        ctx.get_attribute_label(...)
        ctx.resolve_enum_value(...)
        ctx.get_module_output_dir(...)
        ctx.request_repack(...)

    No giant in-RAM entity/relation dicts anymore.
    """

    def __init__(self,
                 date: str,
                 store: PackedStore,
                 enums: dict[str, Any],
                 attr_labeler: AttributeLabeler,
                 data_root: Path,
                 output_root: Path):
        self.date         = date
        self.store        = store
        self.enums        = enums
        self.attr_labeler = attr_labeler

        self.data_root   = data_root        # OUTPUT_ROOT/DATE
        self.output_root = output_root      # meta-viz/data/DATE

        self._repack_jobs: list[dict] = []

    # ---- enums & labels ----

    def get_attribute_label(self, technical_name: str, metadata_label: str | None = None) -> str:
        return self.attr_labeler.get_label(technical_name, metadata_label)

    def resolve_enum_value(self, val):
        """
        Central enum resolver for modules.
        """
        if isinstance(val, str):
            if val.startswith("c_"):
                return self.enums.get(val, val)
            return val
        if isinstance(val, list):
            return [self.resolve_enum_value(x) for x in val]
        return val

    # ---- filesystem helpers ----

    def get_module_output_dir(self, module_type: str, module_name: str | None = None) -> Path:
        """
        Returns a directory where this module can safely write its outputs.
        Right now it's meta-viz/data/<DATE>/<module_type>/.
        """
        base = self.output_root / module_type
        base.mkdir(parents=True, exist_ok=True)
        # If you ever want per-module subdirs, you can use module_name here.
        return base

    # ---- repack orchestration ----

    def request_repack(
        self,
        *,
        profile: str,
        entity_uuids: set[str] | None = None,
        relation_types: set[str] | None = None,
        only_valid: bool | None = None,
    ) -> None:
        """
        Modules call this to request a repack at the end.

        All requests are merged. The final repack always writes to:
            <output_root>/repack

        - entity_uuids: UUID strings to include (unioned).
        - relation_types:
            * None in any job → take ALL reltypes.
            * Otherwise union sets.
        - only_valid:
            * True in ALL jobs → only valid nodes included.
            * Otherwise       → include all nodes.
        """
        self._repack_jobs.append({
            "profile":        profile,
            "entity_uuids":   set(entity_uuids or ()),
            "relation_types": None if relation_types is None else set(relation_types),
            "only_valid":     only_valid,
        })

    @property
    def repack_jobs(self) -> list[dict]:
        return self._repack_jobs


# ---------- create PackedStore ----------

try:
    # adjust this constructor to match your reader implementation
    store = PackedStore(PACKED_DIR)
    print(f"[packed] Loaded PackedStore from {PACKED_DIR}")
except FileNotFoundError as e:
    print(f"[packed] ERROR: {e}")
    print("[packed] You probably need to run the binary packer first.")
    sys.exit(1)

ctx = Context(
    DATE,
    store,
    enums_merged,
    attr_labeler,
    data_root=DATA_DIR_ROOT,
    output_root=METAVIZ_OUTPUT,
)

# ---------- module loading & execution ----------

def load_module_from_path(path: Path):
    module_name = path.stem   # "filename" from "filename.extension"
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


for module_dir in MODULES_DIR.iterdir():
    if not module_dir.is_dir():
        continue

    module_type = module_dir.name
    print(module_type)

    # ensure per-module-type output dir exists: meta-viz/data/<DATE>/<module_type>/
    out_dir = METAVIZ_OUTPUT / module_type
    out_dir.mkdir(parents=True, exist_ok=True)

    # what modules are allowed for this type?
    allowed_for_type = LOADOUT.get(module_type) if LOADOUT else None

    for py_file in module_dir.glob("*.py"):
        module_name = py_file.stem

        # Skip module if restricted by loadout
        if allowed_for_type is not None and module_name not in allowed_for_type:
            continue

        print(f"         Loading module {module_type}/{py_file.name}")

        mod = load_module_from_path(py_file)

        if not hasattr(mod, "run"):
            print(f"    WARNING: {py_file.name} has no run(ctx, out_dir)")
            continue

        print(f"         Running module {module_type}/{py_file.name}")

        module_out_dir = ctx.get_module_output_dir(module_type, module_name)

        # NEW WORLD: modules are responsible for their own I/O.
        # Convention: run(ctx, out_dir) and return value is ignored.
        try:
            mod.run(ctx, module_out_dir)
        except TypeError as e:
            # Helpful error if someone still has an old-style signature.
            print(f"    ERROR: {py_file.name} run() signature must be run(ctx, out_dir). Got TypeError: {e}")
        except Exception as e:
            print(f"    ERROR: exception while running {py_file.name}: {e}")


# ---------- save attribute labels if needed ----------

attr_labeler.save_if_dirty()

# ---------- rebuild index ----------

from rebuild_index import rebuild_index
rebuild_index(DATE)

if ctx.repack_jobs:
    print(f"[repack] {len(ctx.repack_jobs)} repack job(s) requested")
    from repack import run_repack

    merged_uuids: set[str] = set()
    merged_reltypes: None
    all_valid_flags = []

    for job in ctx.repack_jobs:
        merged_uuids |= job["entity_uuids"]

        rt = job["relation_types"]
        if rt is None:
            merged_reltypes = None
        elif merged_reltypes is not None:
            merged_reltypes |= rt

        all_valid_flags.append(job["only_valid"])

    # Determine only_valid
    if all(v is True for v in all_valid_flags):
        final_only_valid = True
    else:
        final_only_valid = None

    # Destination is always meta-viz/data/DATE/repack
    dest_dir = METAVIZ_OUTPUT / "repack"
    dest_dir.mkdir(parents=True, exist_ok=True)

    profile_name = ctx.repack_jobs[0]["profile"]

    print(
        f"[repack] Merged UUID count = {len(merged_uuids)}\n"
        f"[repack] Relation types = "
        f"{'ALL' if merged_reltypes is None else len(merged_reltypes)}\n"
        f"[repack] only_valid = {final_only_valid}\n"
        f"[repack] Output → {dest_dir}"
    )

    run_repack(
        source_root=PACKED_DIR,
        dest_dir=dest_dir,
        profile=profile_name,
        entity_uuids=merged_uuids,
        relation_types=merged_reltypes,
        only_valid=final_only_valid,
    )