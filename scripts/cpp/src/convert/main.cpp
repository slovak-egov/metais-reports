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

    std::cout << "Dictionary values: " << pretty_u64(pre.dict.seen.size()) << "/" << pretty_u64(pre.dict.total_seen) << " total seen\n";
    std::cout << "Objects total: " << pre.total_records << ", missing type: " << pre.missing_type << ", missing attributes: " << pre.missing_attributes << ", bad attributes type: " << pre.bad_attributes_type << "\n";
    pre.total_records = 0; pre.missing_type = 0; pre.missing_attributes = 0; pre.bad_attributes_type = 0;

    prepass("rels", layout, pre, false);
    
    std::cout << "Dictionary values: " << pretty_u64(pre.dict.seen.size()) << "/" << pretty_u64(pre.dict.total_seen) << " total seen\n";
    std::cout << "Relations total: " << pre.total_records << ", missing type: " << pre.missing_type << ", missing attributes: " << pre.missing_attributes << ", bad attributes type: " << pre.bad_attributes_type << "\n";

    auto& cat_ent = pre.attrs_ent;
    auto& cat_rel = pre.attrs_rel;
    auto& dct = pre.dict;

    dct.finalize_sorted();

    std::cout << "Citypes seen: " << cat_ent.object_count_by_type.size() << "\n";
    std::uint64_t ent_ct = 0;
    for (const auto& [t, n] : cat_ent.object_count_by_type) {
        ent_ct += n;

        std::size_t attr_n = 0;
        if (auto it = cat_ent.seen_attrs_by_type.find(t); it != cat_ent.seen_attrs_by_type.end())
            attr_n = it->second.size();

        std::cout << t << ": " << pretty_u64(n)
                << " objects, " << pretty_u64(attr_n) << " attrs\n";
    }
    std::cout << "Total number of objects: " << pretty_u64(ent_ct) << "\n";

    std::cout << "Reltypes seen: " << cat_rel.object_count_by_type.size() << "\n";
    std::uint64_t rel_ct = 0;
    for (const auto& [t, n] : cat_rel.object_count_by_type) {
        rel_ct += n;

        std::size_t attr_n = 0;
        if (auto it = cat_rel.seen_attrs_by_type.find(t); it != cat_rel.seen_attrs_by_type.end())
            attr_n = it->second.size();

        std::cout << t << ": " << pretty_u64(n)
                << " relations, " << pretty_u64(attr_n) << " attrs\n";
    }
    std::cout << "Total number of relations: " << pretty_u64(rel_ct) << "\n";
}