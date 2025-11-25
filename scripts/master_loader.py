import sys, os
from datetime import datetime
from pathlib import Path
import importlib.util
import json
from typing import Any
from tqdm import tqdm
from collections import defaultdict

# script returns entity and relation dictionaries

# entity["types"] stores the citypes list
# entity[type]["metadata"] stores metadata for the given citype
# entity[type][uuid] stores flattened attribute list with uuid, type and metadata folded in

# relation["types"] stores the relations list
# relation["by_node"][(node)]["by_src"/"by_tgt"] (no entity has the same name as relation)
# relation["by_node"]["AS"]["by_src"] = {"AS_SLUZI_KS":..., "AS_realizuje_cloud_KS":..., ...} - AS is a source
# relation["by_node"]["AS"]["by_tgt"] = {"ISVS_realizuje_AS"} - AS is a target
# relation["by_rel"][reltype]["metadata"] stores metadata of a given relation
# relation["by_rel"][reltype][direction][uuid] stores uuids of all entities related to the given uuid by given reltype of the given direction, ex.
# relation["by_rel"]["PO_je_gestor_KS"]["in"][(some KS uuid)]  stores all PO uuids that own a given KS
# relation["by_rel"]["PO_je_gestor_KS"]["out"][(some PO uuid)] stores all KS uuids that are own by a given PO

args = sys.argv

def isint(s):
    try: 
        int(s)
    except ValueError:
        return False
    else:
        return True

def check_date(date_str: str) -> None:
    try:
        datetime.strptime(date_str, "%d-%m-%Y")
    except ValueError:
        print(f"Invalid date '{date_str}': expected format dd-mm-yyyy and a real calendar date")
        sys.exit(1)

if len(args) <= 1:
    print("Missing argument: date in format dd-mm-yyyy")
    sys.exit()
else:
    DATE = sys.argv[1]
    check_date(DATE)
    
PROJECT_ROOT = Path(__file__).resolve().parents[1]

SCRIPT_DIR = Path(__file__).resolve().parent
MODULES_DIR_NAME = os.getenv("MODULES_DIR_NAME", "modules")
MODULES_DIR = SCRIPT_DIR / MODULES_DIR_NAME

OUTPUT_DIR_NAME = os.getenv("METAIS_RAW_OUTPUT_ROOT", "output")

env_path = os.getenv("META_VIZ_DATA_ROOT")

if env_path:
    METAVIZ_OUTPUT_ROOT = (PROJECT_ROOT / env_path).resolve()
else:
    METAVIZ_OUTPUT_ROOT = PROJECT_ROOT / "meta-viz" / "data"

METAVIZ_OUTPUT = METAVIZ_OUTPUT_ROOT / DATE

DATA_DIR_ROOT   = PROJECT_ROOT / OUTPUT_DIR_NAME / DATE
NODES_DIR       = DATA_DIR_ROOT / "nodes"
RELS_DIR        = DATA_DIR_ROOT / "relations"
NODES_META_ROOT = DATA_DIR_ROOT / "metadata"
NODES_META_DIR  = NODES_META_ROOT / "nodes"
RELS_META_DIR   = NODES_META_ROOT / "relations"

exists = True

for dir in [NODES_DIR, RELS_DIR, NODES_META_DIR, RELS_META_DIR]:
    if not os.path.isdir(dir):
        print(f"Directory {dir} does not exist")
        exists = False

if not exists:
    print("One of the directories does not exist. Aborting")
    sys.exit()


### load data ###
def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_result_array(doc: Any):
    if isinstance(doc, dict) and isinstance(doc.get("result"), list):
        return doc["result"]
    if isinstance(doc, dict) and isinstance(doc.get("results"), list):
        return doc["results"]
    if isinstance(doc, list):
        return doc
    raise ValueError("Unrecognized raw JSON format")

entity = {}
relation = {}

# entities
citypes_path = NODES_META_ROOT / "citypes_list.json"
if citypes_path.exists():
    citypes_list = get_result_array(load_json(citypes_path))
    if citypes_list == []:
        print(f"WARNING: {citypes_path} exists but contains no 'results'")
else:
    citypes_list = []
    print(f"WARNING: {citypes_path} does not exist!")

if len(citypes_list) == 0: # fallback - look into nodes directory and grab nodes from the files that are there
    print("List of node types is empty, inferring directly from the nodes folder")
    for node_file in sorted(NODES_DIR.glob("*.json")):
        node_type = node_file.stem
        citypes_list.append({"technicalName" : node_type})

'''
# main entity loader
entity["types"] = citypes_list
for citype in tqdm(citypes_list, desc="Loading citypes", position=0):
    citype_name = citype.get("technicalName", "")
    short_name = (citype_name or "")[:15]
    metadata = load_json(NODES_META_DIR / (citype_name + ".json"))
    entity[citype_name] = {
        "metadata": metadata
    }

    node_data = get_result_array(load_json(NODES_DIR / (citype_name + ".json")))

    for node_entity in tqdm(
        node_data,
        desc=f"  {short_name}",
        leave=False,
        position=1,
    ):
        entity_uuid = node_entity.get("uuid") # everything has to have a uuid
        flat_data = {}
        for key, value in node_entity.items():
            if key == "attributes":
                for entry in value:
                    flat_data[entry["name"]] = entry["value"]
            else:
                flat_data[key] = value
        entity[citype_name][entity_uuid] = flat_data
'''

# helper to share mem
def intern_str(x):
    if isinstance(x, str):
        return sys.intern(x)
    return x

entity["types"] = citypes_list
for citype in tqdm(citypes_list, desc="Loading citypes", position=0):
    citype_name = citype.get("technicalName", "")
    short_name = (citype_name or "")[:15]

    metadata  = load_json(NODES_META_DIR / (citype_name + ".json"))
    node_data = get_result_array(load_json(NODES_DIR / (citype_name + ".json")))

    # ---------- PASS 1: build attribute schema ----------
    columns: list[str] = []
    index_by_name: dict[str, int] = {}

    # metaAttributes schema (dynamic)
    meta_keys: list[str] = []
    meta_schema: dict[str, int] = {}

    for node_entity in node_data:
        # normal attributes
        for entry in node_entity.get("attributes", []):
            attr_name = entry.get("technicalName") or entry.get("name")
            if not attr_name:
                continue
            if attr_name not in index_by_name:
                index_by_name[attr_name] = len(columns)
                columns.append(attr_name)

        # metaAttributes
        meta = node_entity.get("metaAttributes") or {}
        for mk in meta.keys():
            if mk not in meta_schema:
                meta_schema[mk] = len(meta_keys)
                meta_keys.append(mk)

    n_rows = len(node_data)

    # ---------- Allocate column storage ----------
    # attrs: cols[col_idx][row_idx] = value
    cols: list[list] = [[None] * n_rows for _ in columns]

    # uuids
    uuids: list[str | None] = [None] * n_rows
    uuid_to_index: dict[str, int] = {}

    # meta: meta_cols[meta_col_idx][row_idx] = value
    meta_cols: list[list] = [[None] * n_rows for _ in meta_keys]

    # ---------- PASS 2: fill data ----------
    for row_idx, node_entity in enumerate(
        tqdm(node_data, desc=f"  {short_name}", leave=False, position=1)
    ):
        entity_uuid = node_entity.get("uuid")
        if not entity_uuid:
            continue  # or raise

        entity_uuid = intern_str(entity_uuid)
        uuids[row_idx] = entity_uuid
        uuid_to_index[entity_uuid] = row_idx

        # attributes -> columnar
        for entry in node_entity.get("attributes", []):
            attr_name = entry.get("technicalName") or entry.get("name")
            if not attr_name:
                continue
            col_idx = index_by_name.get(attr_name)
            if col_idx is None:
                continue  # shouldn't happen if pass 1 saw everything
            value = intern_str(entry.get("value"))
            cols[col_idx][row_idx] = value

        # metaAttributes -> dynamic columnar
        meta = node_entity.get("metaAttributes") or {}
        for mk, mv in meta.items():
            col_idx = meta_schema.get(mk)
            if col_idx is None:
                # shouldn't happen if pass 1 saw everything, but be safe
                continue
            meta_cols[col_idx][row_idx] = intern_str(mv) if mv is not None else None

    # let node_data be GC'd ASAP
    del node_data

    # ---------- Store in master entity dict ----------
    entity[citype_name] = {
        "metadata": metadata,

        # attributes
        "columns": columns,
        "schema": index_by_name,
        "uuids": uuids,
        "uuid_to_index": uuid_to_index,
        "cols": cols,

        # metaAttributes (dynamic)
        "meta_keys": meta_keys,      # list of names
        "meta_schema": meta_schema,  # name -> column index
        "meta_cols": meta_cols,      # [meta_col_idx][row_idx] -> value
    }

# relations
reltypes_path = NODES_META_ROOT / "reltypes_list.json"
if reltypes_path.exists():
    reltypes_list = get_result_array(load_json(reltypes_path))
    if reltypes_list == []:
        print(f"WARNING: reltypes_list.json exists but contains no 'results'")
else:
    reltypes_list = []

if len(reltypes_list) == 0:
    print("List of relation types is empty, inferring directly from the relations folder")
    for rel_file in RELS_DIR.glob("*.json"):
        rel_type = rel_file.stem
        reltypes_list.append({"technicalName" : rel_type})

# main relation loader
relation["types"] = reltypes_list
relation["by_node"] = {}
relation["by_rel"] = {}
for reltype in tqdm(reltypes_list, desc="Loading reltypes", position=0):
    reltype_name = reltype.get("technicalName", "")
    if not reltype_name:
        continue

    short_name = (reltype_name or "")[:15]

    metadata = load_json(RELS_META_DIR / f"{reltype_name}.json")
    sources = metadata.get("sources") or []
    targets = metadata.get("targets") or []
    if not sources or not targets:
        print(f"WARNING: {reltype_name} has no sources/targets in metadata")
        continue

    source_type = sources[0].get("technicalName")
    target_type = targets[0].get("technicalName") # source_type ---rel---> target_type

    relation["by_rel"][reltype_name] = {
        "metadata": metadata,
        "source_type": source_type,
        "target_type": target_type,
        "by_src": {},
        "by_tgt": {}
    }
    if source_type not in relation["by_node"]:
        relation["by_node"][source_type] = { "by_src": set(), "by_tgt": set() }
    if target_type not in relation["by_node"]:
        relation["by_node"][target_type] = { "by_src": set(), "by_tgt": set() }
    relation["by_node"][source_type]["by_src"].add(reltype_name)
    relation["by_node"][target_type]["by_tgt"].add(reltype_name)

    rel_doc = load_json(RELS_DIR / f"{reltype_name}.json")
    rows = rel_doc.get("result", [])

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

        src_map = relation["by_rel"][reltype_name]["by_src"]
        tgt_map = relation["by_rel"][reltype_name]["by_tgt"]

        src_map.setdefault(sid, []).append(tid)
        tgt_map.setdefault(tid, []).append(sid)

# measure size
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

print("entity size:", deep_size(entity) / 1024**2, "MB")
print("relation size:", deep_size(relation) / 1024**2, "MB")

# load data into context
class Context:
    def __init__(self, date, entity, relation):
        self.date = date
        self.entity = entity
        self.relation = relation

    def get_entity_attr(self, ctype, uuid, attr_name):
        data = self.entity[ctype]
        idx = data["uuid_to_index"][uuid]
        col_idx = data["schema"][attr_name]
        return data["cols"][col_idx][idx]

    def get_entity_metaAttr(self, ctype, uuid, metaAttr_name):
        """
        Return a single metaAttribute value, or None if
        that meta key doesn't exist for this citype / entity.
        """
        data = self.entity[ctype]
        idx = data["uuid_to_index"][uuid]

        meta_schema = data.get("meta_schema", {})
        col_idx = meta_schema.get(metaAttr_name)
        if col_idx is None:
            return None

        meta_cols = data.get("meta_cols", [])
        if col_idx >= len(meta_cols):
            return None

        return meta_cols[col_idx][idx]

    def get_entity_record(self, ctype, uuid, include_null=False):
        """
        Return a dict with:
          - type
          - uuid
          - attributes: {attr_name: value}
          - metaAttributes: {meta_key: value}
        """
        data = self.entity[ctype]
        idx = data["uuid_to_index"][uuid]

        # attributes
        attrs = {}
        for attr_name, col_idx in data["schema"].items():
            val = data["cols"][col_idx][idx]
            if val is None and not include_null:
                continue
            attrs[attr_name] = val

        # metaAttributes
        meta = {}
        meta_schema = data.get("meta_schema", {})
        meta_cols   = data.get("meta_cols", [])

        for mk, col_idx in meta_schema.items():
            if col_idx >= len(meta_cols):
                continue
            val = meta_cols[col_idx][idx]
            if val is None and not include_null:
                continue
            meta[mk] = val

        return {
            "type": ctype,
            "uuid": uuid,
            "attributes": attrs,
            "metaAttributes": meta,
        }

ctx = Context(DATE, entity, relation)

def load_module_from_path(path: Path):
    module_name = path.stem   # "filename" from "filename.extension"
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod

for module_dir in MODULES_DIR.iterdir():
    if module_dir.is_dir():
        module_type = module_dir.name
        print(module_type)

        # create per-module-type output dir: data/<DATE>/<module_type>/
        out_dir = METAVIZ_OUTPUT / module_type
        out_dir.mkdir(parents=True, exist_ok=True)

        for py_file in module_dir.glob("*.py"):
            print(f"  Loading module {module_type}/{py_file.name}")

            mod = load_module_from_path(py_file)

            if hasattr(mod, "run"):
                print(f"  Running module {module_type}/{py_file.name}")
                run_result = mod.run(ctx)

                # assume run_result is JSON-serializable (dict/list/str/etc.)
                out_path = out_dir / f"{py_file.stem}.json"
                try:
                    with out_path.open("w", encoding="utf-8") as f:
                        json.dump(run_result, f, ensure_ascii=False, indent=2)
                    print(f"    -> wrote {out_path}")
                except TypeError as e:
                    print(f"    !! FAILED to JSON-serialize result from {py_file}: {e}")
            else:
                print(f"    WARNING: {py_file.name} has no run(ctx)")