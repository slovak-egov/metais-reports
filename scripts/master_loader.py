#!/usr/bin/env python3
import sys, os
from sys import intern as _intern
from datetime import datetime
from pathlib import Path
import importlib.util
import json
from typing import Any
from tqdm import tqdm
import argparse
import gzip
from json_writer import dump_json_smart
from config_env import find_project_root, load_env_file
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


# for saving the packed relations (sets) to the drive
class SetEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, set):
            return {"__set__": list(obj)}
        return json.JSONEncoder.default(self, obj)


def set_decoder(obj):
    if "__set__" in obj:
        return set(obj["__set__"])
    return obj

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
    description="MetaIS snapshot processor + meta-viz data generator (raw-ish entity)"
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
parser.add_argument(
    "--repack",
    action="store_true",
    help="Rebuild and overwrite packed entity/relation cache even if present",
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

# ---------- now the rest that depends on DATE ----------

METAVIZ_OUTPUT  = METAVIZ_OUTPUT_ROOT / DATE

DATA_DIR_ROOT   = PROJECT_ROOT / OUTPUT_DIR_NAME / DATE
NODES_DIR       = DATA_DIR_ROOT / "nodes"
RELS_DIR        = DATA_DIR_ROOT / "relations"
METADATA_ROOT   = DATA_DIR_ROOT / "metadata"
NODES_META_DIR  = METADATA_ROOT / "nodes"
RELS_META_DIR   = METADATA_ROOT / "relations"

PACKED_ENTITY_PATH   = DATA_DIR_ROOT / "packed_entity_raw.json.gz"
PACKED_RELATION_PATH = DATA_DIR_ROOT / "packed_relation.json.gz"

ENUMS_MERGED_PATH = METADATA_ROOT / "enums_merged.json"

exists = True
for dir_ in [NODES_DIR, RELS_DIR, NODES_META_DIR, RELS_META_DIR]:
    if not os.path.isdir(dir_):
        print(f"Directory {dir_} does not exist")
        exists = False

if not exists:
    print("One of the directories does not exist. Aborting")
    sys.exit(1)

### load data ###

def open_json_or_gz(path: Path, mode: str = "rt", encoding: str = "utf-8"):
    """
    Open a JSON file that might be plain .json or gzipped (.gz).
    If `path` has a suffix .gz, we use gzip; otherwise normal open().
    """
    if path.suffix == ".gz":
        return gzip.open(path, mode, encoding=encoding)
    return path.open(mode, encoding=encoding)


def load_json_or_gz(path: Path, stem: str | None = None) -> Any:
    """
    Load JSON from either a .json or .json.gz file.

    Usage:
      - load_json_or_gz(Path(".../KS.json"))
      - load_json_or_gz(dir_path, "KS")  -> dir_path/KS.json or KS.json.gz
    """
    if stem is not None:
        base = path.parent / stem
    else:
        base = path
        if base.suffix == ".gz":
            base = base.with_suffix("")  # KS.json.gz -> KS.json

    json_path = base
    if json_path.suffix != ".json":
        json_path = json_path.with_suffix(".json")

    gz_path = json_path.with_suffix(json_path.suffix + ".gz")  # .json.gz

    if json_path.is_file():
        with json_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    if gz_path.is_file():
        with gzip.open(gz_path, "rt", encoding="utf-8") as f:
            return json.load(f)

    raise FileNotFoundError(f"Neither {json_path} nor {gz_path} exists")


def get_result_array(doc: Any):
    if isinstance(doc, dict) and isinstance(doc.get("result"), list):
        return doc["result"]
    if isinstance(doc, dict) and isinstance(doc.get("results"), list):
        return doc["results"]
    if isinstance(doc, list):
        return doc
    raise ValueError("Unrecognized raw JSON format")


def intern_all_strings(obj: Any) -> Any:
    """
    Recursively intern all string objects in a nested structure of dicts/lists.
    """
    if isinstance(obj, str):
        return _intern(obj)
    if isinstance(obj, list):
        return [intern_all_strings(x) for x in obj]
    if isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            if isinstance(k, str):
                k2 = _intern(k)
            else:
                k2 = k
            new[k2] = intern_all_strings(v)
        return new
    return obj


entity: dict = {}
relation: dict = {}

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

use_cache = (
    (not cli_args.repack)
    and PACKED_ENTITY_PATH.is_file()
    and PACKED_RELATION_PATH.is_file()
)

# ---------- build or load ENTITY + RELATION ----------

if use_cache:
    print(f"[cache] Loading packed entity and relation for {DATE}")
    with gzip.open(PACKED_ENTITY_PATH, "rt", encoding="utf-8") as f:
        entity = json.load(f)
    entity = intern_all_strings(entity)

    with gzip.open(PACKED_RELATION_PATH, "rt", encoding="utf-8") as f:
        relation = json.load(f, object_hook=set_decoder)
    relation = intern_all_strings(relation)

else:
    print(f"[cache] No valid cache for {DATE} (or --repack given); building from raw files")

    # ---------- ENTITIES: raw-ish structure ----------

    citypes_path = METADATA_ROOT / "citypes_list.json"
    if citypes_path.exists() or (citypes_path.with_suffix(".json.gz")).exists():
        citypes_list = get_result_array(load_json_or_gz(citypes_path))
        if citypes_list == []:
            print(f"WARNING: {citypes_path} exists but contains no 'results'")
    else:
        citypes_list = []
        print(f"WARNING: {citypes_path} does not exist!")

    if len(citypes_list) == 0:
        print("List of node types is empty, inferring directly from the nodes folder")
        for node_file in sorted(NODES_DIR.glob("*.json")):
            node_type = node_file.stem
            citypes_list.append({"technicalName": node_type})

    entity["types"] = citypes_list
    entity["by_uuid"] = {}
    entity["by_type"] = {}
    entity["citype_metadata"] = {}

    # helper to intern
    def intern_str(x):
        if isinstance(x, str):
            return _intern(x)
        return x

    for citype in tqdm(citypes_list, desc="Loading citypes", position=0):
        citype_name = citype.get("technicalName", "")
        if not citype_name:
            continue
        citype_name = intern_str(citype_name)

        short_name = citype_name[:15]

        metadata  = load_json_or_gz(NODES_META_DIR / f"{citype_name}.json")
        node_data = get_result_array(
            load_json_or_gz(NODES_DIR / f"{citype_name}.json")
        )

        entity["citype_metadata"][citype_name] = metadata
        by_type_list = entity["by_type"].setdefault(citype_name, [])
        by_uuid = entity["by_uuid"]

        for node_entity in tqdm(
            node_data,
            desc=f"  {short_name}",
            leave=False,
            position=1,
        ):
            uuid = node_entity.get("uuid")
            if not uuid:
                continue
            uuid = intern_str(uuid)

            raw_attrs = node_entity.get("attributes") or []
            raw_meta  = node_entity.get("metaAttributes") or {}

            # convert attributes list -> dict
            attr_dict: dict[str, Any] = {}
            for entry in raw_attrs:
                if not isinstance(entry, dict):
                    continue
                attr_name = entry.get("technicalName") or entry.get("name")
                if not attr_name:
                    continue
                attr_name = intern_str(attr_name)
                val = entry.get("value")
                if isinstance(val, str):
                    val = intern_str(val)
                attr_dict[attr_name] = val

            meta_dict: dict[str, Any] = {}
            for mk, mv in (raw_meta or {}).items():
                if isinstance(mk, str):
                    mk = intern_str(mk)
                if isinstance(mv, str):
                    mv = intern_str(mv)
                meta_dict[mk] = mv

            rec = {
                "type": citype_name,
                "uuid": uuid,
                "attributes": attr_dict,
                "metaAttributes": meta_dict,
            }

            by_uuid[uuid] = rec
            by_type_list.append(uuid)

        del node_data

    # ---------- RELATIONS: keep by_rel / by_node structure ----------

    reltypes_path = METADATA_ROOT / "reltypes_list.json"
    if reltypes_path.exists():
        reltypes_list = get_result_array(load_json_or_gz(reltypes_path))
        if reltypes_list == []:
            print("WARNING: reltypes_list.json exists but contains no 'results'")
    else:
        reltypes_list = []

    if len(reltypes_list) == 0:
        print("List of relation types is empty, inferring directly from the relations folder")
        for rel_file in RELS_DIR.glob("*.json"):
            rel_type = rel_file.stem
            reltypes_list.append({"technicalName": rel_type})

    relation["types"] = reltypes_list
    relation["by_node"] = {}
    relation["by_rel"] = {}

    for reltype in tqdm(reltypes_list, desc="Loading reltypes", position=0):
        reltype_name = reltype.get("technicalName", "")
        if not reltype_name:
            continue
        reltype_name = intern_str(reltype_name)

        short_name = reltype_name[:15]

        # skip reltypes that don't have a valid relations file
        rel_json = RELS_DIR / f"{reltype_name}.json"
        rel_gz   = RELS_DIR / f"{reltype_name}.json.gz"
        if not rel_json.is_file() and not rel_gz.is_file():
            print(
                f"[WARN] No relations file found for {reltype_name} in {RELS_DIR}, "
                "skipping this relation type."
            )
            continue

        metadata = load_json_or_gz(RELS_META_DIR / f"{reltype_name}.json")
        sources = metadata.get("sources") or []
        targets = metadata.get("targets") or []
        if not sources or not targets:
            print(f"WARNING: {reltype_name} has no sources/targets in metadata")
            continue

        source_type = sources[0].get("technicalName")
        target_type = targets[0].get("technicalName")
        if isinstance(source_type, str):
            source_type = intern_str(source_type)
        if isinstance(target_type, str):
            target_type = intern_str(target_type)

        relation["by_rel"][reltype_name] = {
            "metadata": metadata,
            "source_type": source_type,
            "target_type": target_type,
            "by_src": {},
            "by_tgt": {},
        }
        if source_type not in relation["by_node"]:
            relation["by_node"][source_type] = {"by_src": set(), "by_tgt": set()}
        if target_type not in relation["by_node"]:
            relation["by_node"][target_type] = {"by_src": set(), "by_tgt": set()}

        relation["by_node"][source_type]["by_src"].add(reltype_name)
        relation["by_node"][target_type]["by_tgt"].add(reltype_name)

        rel_doc = load_json_or_gz(RELS_DIR / f"{reltype_name}.json")
        rows = rel_doc.get("result", [])

        by_src = relation["by_rel"][reltype_name]["by_src"]
        by_tgt = relation["by_rel"][reltype_name]["by_tgt"]

        for relation_pair in tqdm(
            rows,
            desc=f"  {short_name}",
            leave=False,
            position=1,
        ):
            tid = relation_pair.get("target")
            sid = relation_pair.get("source")
            if not sid or not tid:
                continue
            if isinstance(sid, str):
                sid = intern_str(sid)
            if isinstance(tid, str):
                tid = intern_str(tid)

            by_src.setdefault(sid, []).append(tid)
            by_tgt.setdefault(tid, []).append(sid)

        del rel_doc

    # ---------- SAVE CACHE ----------

    try:
        with gzip.open(PACKED_ENTITY_PATH, "wt", encoding="utf-8") as f:
            json.dump(entity, f, ensure_ascii=False, separators=(",", ":"))
        with gzip.open(PACKED_RELATION_PATH, "wt", encoding="utf-8") as f:
            json.dump(relation, f, ensure_ascii=False, separators=(",", ":"), cls=SetEncoder)
        print(f"[cache] Wrote {PACKED_ENTITY_PATH} and {PACKED_RELATION_PATH}")
    except Exception as e:
        print(f"[cache] WARNING: failed to write packed cache: {e}")

# ---------- small size introspection (optional) ----------

def deep_size(obj, seen=None):
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)

    size = sys.getsizeof(obj)

    if isinstance(obj, dict):
        size += sum(deep_size(k, seen) + deep_size(v, seen)
                    for k, v in obj.items())
    elif isinstance(obj, (list, tuple, set)):
        size += sum(deep_size(i, seen) for i in obj)

    return size
'''
print("entity size:", deep_size(entity) / 1024**2, "MB")
print("relation size:", deep_size(relation) / 1024**2, "MB")

def mb(x): return deep_size(x) / 1024**2

print("---- entity breakdown ----")
print("types:", mb(entity.get("types", [])), "MB")
print("by_uuid:", mb(entity.get("by_uuid", {})), "MB")
print("by_type:", mb(entity.get("by_type", {})), "MB")
print("citype_metadata:", mb(entity.get("citype_metadata", {})), "MB")
print("--------------------------")
'''
# ---------- Context over raw-like structure ----------

class Context:
    def __init__(self, date, entity, relation, enums, attr_labeler):
        self.date = date
        self.entity = entity
        self.relation = relation
        self.enums = enums
        self.attr_labeler = attr_labeler

    def get_entity_identifier(
        self,
        uuid: str,
        rec: dict,
        name_attr: str = "Gen_Profil_nazov",
        code_attr: str = "Gen_Profil_kod_metais",
    ) -> str:
        """
        Human-readable identifier:
          1) name_attr (default: Gen_Profil_nazov)
          2) code_attr (default: Gen_Profil_kod_metais)
          3) uuid
        """
        name = self.get_attr_value_any(rec, name_attr)
        code = self.get_attr_value_any(rec, code_attr)

        if name:
            return str(name)
        if code:
            return str(code)
        return uuid

    def get_entity_type(self, uuid: str) -> str:
        return self.entity["by_uuid"][uuid]["type"]

    def get_entity_attr(self, ctype: str, uuid: str, attr_name: str):
        rec = self.entity["by_uuid"].get(uuid)
        if not rec:
            raise KeyError(f"UUID {uuid} not found")
        # optional sanity check
        # if rec["type"] != ctype:
        #     raise KeyError(f"UUID {uuid} is not of type {ctype} (has {rec['type']})")
        return rec.get("attributes", {}).get(attr_name)

    def get_entity_metaAttr(self, ctype: str, uuid: str, metaAttr_name: str):
        rec = self.entity["by_uuid"].get(uuid)
        if not rec:
            raise KeyError(f"UUID {uuid} not found")
        return rec.get("metaAttributes", {}).get(metaAttr_name)

    def get_entity_record(self, ctype: str, uuid: str, include_null: bool = False):
        rec = self.entity["by_uuid"].get(uuid)
        if not rec:
            raise KeyError(f"UUID {uuid} not found")
        # Same note as before: we don't have the full schema,
        # so include_null doesn't really change anything.
        # We just return the stored record.
        return rec

    def get_attr_value_any(self, rec: dict, technical_name: str):
        """
        Get an attribute value from either:
          - raw dict rec["attributes"][technical_name]
          - normalized list rec["attributes"] = [{attributeTechnicalName, value}, ...]
        """
        attrs = rec.get("attributes")
        if isinstance(attrs, dict):
            return attrs.get(technical_name)
        if isinstance(attrs, list):
            for a in attrs:
                if a.get("attributeTechnicalName") == technical_name:
                    return a.get("value")
        return None

    def normalize_attributes(self, citype_name: str, attrs: dict):
        """
        Convert a raw attributes dict into an ordered list with labels:

          { "Gen_Profil_nazov": "...", ... }

        ->

          [
            {
              "attributeTechnicalName": "Gen_Profil_nazov",
              "attributeName": "Názov",
              "value": "...",
            },
            ...
          ]

        Uses attributes + attributeProfiles[*].attributes from metadata.
        """
        from typing import Any

        if not isinstance(attrs, dict):
            return attrs

        meta_defs = list(self.iter_all_attr_defs_for_citype(citype_name))
        result: list[dict[str, Any]] = []
        used: set[str] = set()

        # 1) follow metadata order
        for attr_def in meta_defs:
            tech = attr_def.get("technicalName")
            if not tech or tech not in attrs:
                continue

            meta_label = (
                attr_def.get("name")
                or attr_def.get("engName")
                or tech
            )

            raw_value = attrs[tech]
            value = self.resolve_enum_value(raw_value)

            result.append({
                "attributeTechnicalName": tech,
                "attributeName": self.get_attribute_label(tech, meta_label),
                "value": value,
            })
            used.add(tech)

        # 2) leftover attributes (no metadata)
        for tech in sorted(k for k in attrs.keys() if k not in used):
            raw_value = attrs[tech]
            value = self.resolve_enum_value(raw_value)

            result.append({
                "attributeTechnicalName": tech,
                "attributeName": self.get_attribute_label(tech, None),
                "value": value,
            })

        return result

    def iter_uuids_of_type(self, ctype: str):
        for uuid in self.entity["by_type"].get(ctype, []):
            yield uuid

    def get_citype_metadata(self, citype_name: str) -> dict:
        """
        Return raw metadata dict for a citype, or {} if missing.
        """
        return (self.entity.get("citype_metadata") or {}).get(citype_name, {}) or {}

    def iter_all_attr_defs_for_citype(self, citype_name: str):
        """
        Yield all attribute definition dicts for a given citype from:
          - metadata['attributes']
          - metadata['attributeProfiles'][*]['attributes']
        """
        meta = self.get_citype_metadata(citype_name)

        # 1) top-level attributes
        for attr in meta.get("attributes") or []:
            yield attr

        # 2) attributes under profiles (EA_Profil_ISVS, etc.)
        for profile in meta.get("attributeProfiles") or []:
            for attr in profile.get("attributes") or []:
                yield attr

    def get_attribute_label(self, technical_name: str, metadata_label: str | None = None) -> str:
        """
        Central helper for human-readable attribute names.

        Resolution order (inside AttributeLabeler):

          1) metadata_label (if provided)
          2) params file (attribute_labels.json)
          3) interactive prompt (if enabled)
          4) technical_name
        """
        return self.attr_labeler.get_label(technical_name, metadata_label)

    def resolve_enum_value(self, val):
        """
        Optional central enum resolver if you want to use it from modules.
        """
        from collections.abc import Iterable

        if isinstance(val, str):
            if val.startswith("c_"):
                return self.enums.get(val, val)
            return val
        if isinstance(val, list):
            return [self.resolve_enum_value(x) for x in val]
        return val

ctx = Context(DATE, entity, relation, enums_merged, attr_labeler)

# ---------- module loading & execution ----------

def load_module_from_path(path: Path):
    module_name = path.stem   # "filename" from "filename.extension"
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod

for module_dir in MODULES_DIR.iterdir():
    if not module_dir.is_dir():
        continue

    module_type = module_dir.name
    print(module_type)

    # create per-module-type output dir: meta-viz/data/<DATE>/<module_type>/
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

        if hasattr(mod, "run"):
            print(f"         Running module {module_type}/{py_file.name}")
            run_result = mod.run(ctx)

            out_path = out_dir / f"{py_file.stem}.json"
            try:
                with out_path.open("w", encoding="utf-8") as f:
                    print(f"Saving module output to {module_type}/{module_name}.json")
                    if dump_json_smart is not None:
                        # global smart writer – you can tune max_width / indent here
                        dump_json_smart(run_result, f, max_width=80, indent=2, ensure_ascii=False)
                    else:
                        # fallback if json_writer not available
                        json.dump(run_result, f, ensure_ascii=False, indent=2)
                    print(f"    -> wrote {out_path}")
            except TypeError as e:
                print(f"    !! FAILED to JSON-serialize result from {py_file}: {e}")
        else:
            print(f"    WARNING: {py_file.name} has no run(ctx)")

attr_labeler.save_if_dirty()

from rebuild_index import rebuild_index
rebuild_index(DATE)