#include "directory_layout.h"
#include "paths_config.h"
#include "project_root.h"
#include "prepass.h"
#include "freeze_schema.h"
#include "packed_bootstrap.h"
#include "step_marker.h"

#include <iostream>
#include <cstdint>

inline std::string pretty_u64(std::uint64_t v) {
    std::string s = std::to_string(v);
    for (std::ptrdiff_t i = (std::ptrdiff_t)s.size() - 3; i > 0; i -= 3)
        s.insert((size_t)i, ",");
    return s;
}

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

    bootstrap_packed_root(layout);

    const bool pass1_5_done = is_done(layout.packed_root, ".pass1_5.done") && pass1_5_outputs_ok(layout);

    if (pass1_5_done) {
        std::cerr << "[main] Pass 1.5 already done; skipping prepass + freeze.\n";
    }
    else {
        PrepassResult pre;
        prepass("nodes", layout, pre, false);
        prepass("rels",  layout, pre, false);

        freeze_schema_and_build_resolvers(layout, pre);
    }

}