import sys, os
from datetime import datetime
from pathlib import Path
import importlib.util
import json
from typing import Any
from tqdm import tqdm

# script returns entity and relation dictionaries

# entity["types"] stores the citypes list
# entity[type]["metadata"] stores metadata for the given citype
# entity[type][uuid] stores flattened attribute list with uuid, type and metadata folded in

# relation["types"] stores the relations list
# relation["by_node"][(node)]["src"/"tgt"] (no entity has the same name as relation)
# relation["by_node"]["AS"]["src"] = {"AS_SLUZI_KS":..., "AS_realizuje_cloud_KS":..., ...} - AS is a source
# relation["by_node"]["AS"]["tgt"] = {"ISVS_realizuje_AS"} - AS is a target
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

OUTPUT_DIR_NAME = os.getenv("METAIS_RAW_ROOT", "output")

METAVIZ_OUTPUT_ROOT = os.getenv("META_VIZ_DATA_ROOT", "data")
METAVIZ_OUTPUT = PROJECT_ROOT / "data" / DATE

DATA_DIR_ROOT  = PROJECT_ROOT / OUTPUT_DIR_NAME / DATE
NODES_DIR      = DATA_DIR_ROOT / "nodes"
RELS_DIR       = DATA_DIR_ROOT / "relations"
NODES_META_DIR = DATA_DIR_ROOT / "metadata/nodes"
RELS_META_DIR  = DATA_DIR_ROOT / "metadata/relations"

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
citypes_path = NODES_META_DIR / "citypes_list.json"
if citypes_path.exists():
    citypes_list = get_result_array(load_json(citypes_path))
    if citypes_list == []:
        print(f"WARNING: citypes_list.json exists but contains no 'results'")
else:
    citypes_list = []

if len(citypes_list) == 0: # fallback - look into nodes directory and grab nodes from the files that are there
    print("List of node types is empty, inferring directly from the nodes folder")
    for node_file in sorted(NODES_DIR.glob("*.json")):
        node_type = node_file.stem
        citypes_list.append({"technicalName" : node_type})

# main entity loader
entity["types"] = citypes_list
for citype in tqdm(citypes_list, desc="Loading citypes", position=0):
    citype_name = citype.get("technicalName", "")
    short_name = (citype_name or "")[:15]
    metadata = load_json(NODES_META_DIR / ("citype_" + citype_name + ".json"))
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

print(entity["KS"]["fec42cf6-499a-40f0-93d6-143addabe1f5"])

# relations
reltypes_path = RELS_META_DIR / "reltypes_list.json"
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
        "by_source": {},
        "by_target": {}
    }
    if source_type not in relation["by_node"]:
        relation["by_node"][source_type] = { "src": set(), "tgt": set() }
    if target_type not in relation["by_node"]:
        relation["by_node"][target_type] = { "src": set(), "tgt": set() }
    relation["by_node"][source_type]["src"].add(reltype_name)
    relation["by_node"][target_type]["tgt"].add(reltype_name)

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

        src_map = relation["by_rel"][reltype_name]["by_source"]
        tgt_map = relation["by_rel"][reltype_name]["by_target"]

        src_map.setdefault(sid, []).append(tid)
        tgt_map.setdefault(tid, []).append(sid)

# load data into context
class Context:
    def __init__(self):
        pass

ctx = Context()
ctx.date = DATE
ctx.entity = entity
ctx.relation = relation

def load_module_from_path(path: Path):
    module_name = path.stem   # "flatten" from "flatten.py"
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