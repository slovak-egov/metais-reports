#include "directory_layout.h"
#include "paths_config.h"
#include "project_root.h"
#include "data_catalog.h"
#include "prepass.h"

#include <iostream>
#include <cstdint>

inline std::string pretty_u64(std::uint64_t v) {
    std::string s = std::to_string(v);
    for (std::ptrdiff_t i = s.size() - 3; i > 0; i -= 3)
        s.insert(i, ",");
    return s;
}

int main() {
    using namespace metais;

    const auto root = metais::find_project_root();
    const auto cfg  = metais::load_paths_config("config/json/paths.json");
    DirectoryLayout layout(cfg, "16-12-2025", root);

    PrepassResult pre;

    prepass("nodes", layout, pre, false);
    prepass("rels",  layout, pre, false);

    std::cout << "Dictionary values: " << pretty_u64(pre.dict.seen.size())
            << "/" << pretty_u64(pre.dict.total_seen) << " total seen\n";

    std::cout << "Nodes total: " << pretty_u64(pre.nodes.total_records)
            << ", missing type: " << pretty_u64(pre.nodes.missing_type)
            << ", missing attrs: " << pretty_u64(pre.nodes.missing_attributes)
            << ", bad attr type: " << pretty_u64(pre.nodes.bad_attributes_type)
            << ", missing uuid: " << pretty_u64(pre.nodes.missing_uuid)
            << ", bad uuid: " << pretty_u64(pre.nodes.bad_uuid)
            << "\n";

    std::cout << "Rels total: " << pretty_u64(pre.rels.total_records)
            << ", missing type: " << pretty_u64(pre.rels.missing_type)
            << ", missing attrs: " << pretty_u64(pre.rels.missing_attributes)
            << ", bad attr type: " << pretty_u64(pre.rels.bad_attributes_type)
            << "\n";

    pre.dict.finalize_sorted();

}