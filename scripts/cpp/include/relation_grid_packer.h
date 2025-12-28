#pragma once
#include "directory_layout.h"
#include "binary_formats.h"
#include "dict_lookup.h"
#include "global_uuid_index.h"
#include "resolver_index.h"
#include "canonical_value.h"

#include <nlohmann/json.hpp>
#include <unordered_map>
#include <unordered_set>
#include <fstream>
#include <vector>
#include <string>

namespace metais {

    class RelationGridPacker {
    public:
        RelationGridPacker(
            const DirectoryLayout& layout,
            const DictLookup& dict,
            const GlobalUuidIndex& gu,
            const GlobalResolverIndex& resolver,
            const std::vector<std::string>& citypes
        );

        void ingest(const nlohmann::json& j);
        void finalize();

    private:
        struct RelState {
            std::string reltype;
            std::unordered_map<std::string, std::uint32_t> attr_index;
            std::uint32_t A = 0;
            std::uint64_t count = 0;

            std::ofstream attr;  // append-only (grid rows)
            std::ofstream meta;  // append-only (6 cells)
            std::ofstream edges; // append-only (U32 src, U32 tgt)

            std::unordered_set<std::string> source_types;
            std::unordered_set<std::string> target_types;

            bool done = false;
        };

        const DirectoryLayout& layout_;
        const DictLookup& dict_;
        const GlobalUuidIndex& gu_;
        const GlobalResolverIndex& resolver_;
        const std::vector<std::string>& citypes_;

        std::unordered_map<std::string, RelState> cache_;

        static constexpr const char* kMetaKeys[6] = {
            "owner","state","createdBy","createdAt","lastModifiedBy","lastModifiedAt"
        };

        RelState& get_reltype(const std::string& reltype);

        static std::unordered_map<std::string, std::uint32_t>
        load_attr_index(const fs::path& attributes_json);

        static bool rel_done_marker_exists(const fs::path& rel_dir);

        static void write_endpoints_json_atomic(const fs::path& rel_dir, const RelState& st);
    };

}