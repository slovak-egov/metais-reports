// prepass.h
#pragma once
#include "directory_layout.h"
#include "data_catalog.h"
#include "binary_formats.h"

#include <cstdint>
#include <vector>
#include <string>

namespace metais {

    struct PrepassStats {
        std::uint64_t total_records       = 0;
        std::uint64_t missing_type        = 0;

        std::uint64_t missing_attributes  = 0;
        std::uint64_t bad_attributes_type = 0;

        std::uint64_t missing_uuid        = 0;
        std::uint64_t bad_uuid            = 0;
    };

    struct PrepassResult {
        PrepassStats nodes;
        PrepassStats rels;

        AttributeCatalog attrs_ent, attrs_rel;
        AttributeCatalog metaAttrs_ent, metaAttrs_rel;

        ValueDictionary dict;

        std::vector<Uuid128> uuids_ent;
        
        std::unordered_map<std::string, std::vector<Uuid128>> uuids_by_citype;
    };

    void prepass(std::string tag, const DirectoryLayout& layout, PrepassResult& out, bool skip_bad_json);

}