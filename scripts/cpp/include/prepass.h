#pragma once
#include "directory_layout.h"
#include "data_catalog.h"

namespace metais {
    struct PrepassResult {
        std::uint64_t total_records       = 0;
        std::uint64_t missing_type        = 0;
        std::uint64_t missing_attributes  = 0;
        std::uint64_t bad_attributes_type = 0;

        AttributeCatalog attrs_ent, attrs_rel;
        AttributeCatalog metaAttrs_ent, metaAttrs_rel;
        ValueDictionary  dict;
    };

    void prepass(std::string tag, const DirectoryLayout& layout, PrepassResult& prepass_result, bool skip_bad_json);
}