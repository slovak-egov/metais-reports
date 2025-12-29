#include "directory_layout.h"
#include "paths_config.h"
#include "project_root.h"
#include "step_marker.h"

#include "packed_bootstrap.h"
#include "prepass.h"
#include "freeze_schema.h"
#include "pass2_pack.h"
#include "pass3_finalize_relations.h"
#include "pass3_optimize_attributes.h"

#include <iostream>
#include <cstdint>

static bool pass1_5_outputs_ok(const metais::DirectoryLayout& layout) {
    using namespace std::filesystem;
    return exists(layout.dict_dir / "dict.bin")
        && exists(layout.dict_dir / "dict.offsets.bin")
        && exists(layout.dict_dir / "meta.json")
        && exists(layout.uuids_dir / "citypes.json")
        && exists(layout.uuids_dir / "uuids.bin")
        && exists(layout.uuids_dir / "resolver.bin");
}

int main(int argc, char** argv) {

    using namespace metais;

    if (argc != 2) {
        std::cerr << "Usage: metais_convert <DD-MM-YYYY>\n";
        return 2;
    }
    const std::string date = argv[1];

    const auto root = metais::find_project_root();
    const auto cfg  = metais::load_paths_config("config/json/paths.json");
    DirectoryLayout layout(cfg, date, root);

    // 0
    bootstrap_packed_root(layout);

    const bool pass1_5_done = is_done(layout.packed_root, ".pass1_5.done") && pass1_5_outputs_ok(layout);

    if (pass1_5_done) {
        std::cerr << "[main] Pass 1.5 already done; skipping prepass + freeze.\n";
    }
    else {
        PrepassResult pre;
        // 1
        prepass("nodes", layout, pre, false);
        prepass("rels",  layout, pre, false);

        // 1.5
        freeze_schema_and_build_resolvers(layout, pre);
    }

    // 2
    pass2_pack_nodes_and_relations(layout, /*skip_bad_json=*/false);

    // 3
    pass3_finalize_relation_edges(layout);

    // 3 - convert grid to sparse if it takes less space
    pass3_optimize_attributes(layout);

    mark_done(layout.packed_root, ".pass3.done", "pass=3\n");

}