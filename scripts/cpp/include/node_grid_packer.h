#pragma once
#include "directory_layout.h"
#include "binary_formats.h"
#include "dict_lookup.h"
#include "canonical_value.h"

#include <nlohmann/json.hpp>
#include <unordered_map>
#include <vector>
#include <fstream>

namespace metais {

    class NodeGridPacker {
    public:
        NodeGridPacker(const DirectoryLayout& layout, const DictLookup& dict);

        // feed one raw node object
        void ingest(const nlohmann::json& j);

        // finalize all open citype packers (rename tmp -> final, write done markers)
        void finalize();

    private:
        struct CitypeState {
            std::string citype;
            std::vector<Uuid128> uuids; // sorted
            std::unordered_map<std::string, std::uint32_t> attr_index; // technicalName -> index
            std::uint32_t A = 0;      // attributeCount
            std::uint64_t N = 0;      // entity count
            std::fstream attr;        // random write
            std::fstream meta;        // random write
            bool done = false;
        };

        const DirectoryLayout& layout_;
        const DictLookup& dict_;

        std::unordered_map<std::string, CitypeState> cache_;

        static constexpr const char* kMetaKeys[6] = {
            "owner","state","createdBy","createdAt","lastModifiedBy","lastModifiedAt"
        };

        CitypeState& get_citype(const std::string& citype);
        void ensure_preallocated(CitypeState& st);

        static std::unordered_map<std::string, std::uint32_t>
        load_attr_index(const fs::path& attributes_json);

        static std::vector<Uuid128> load_uuids(const fs::path& uuids_bin);

        static void prefill_file_with_ff(const fs::path& tmp_path, std::uint64_t bytes);

        static bool citype_done_marker_exists(const fs::path& citype_dir);
    };

}