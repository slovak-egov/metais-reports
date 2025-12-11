#pragma once

#include <filesystem>
#include <string>
#include <vector>

#include "directory_layout.h"
#include "URI_config.h"

namespace fs = std::filesystem;

// 1) fetch enum list
//   a) inside "results" (list)
//   b) dict containing "code" (this is the enum name) enum_name = res["code"]
//   c) only fetch enum that has res["valid"] (true)
// 2) iterate through enum list and fetch individual enums
//   a) for enum_name in enum_names...
// 3) keep a global set of enum key -> value. report if duplicate.
// URIs for enums are defined in URI.json (all URIs are)
// dir structure:
// layout.enums_dir - root enums dir
// metadata/enums/valid <- individual enums as CODE.json
// metadata/enums <- global merged enum
// metadata/enums <- enum conflicts

namespace metais {

    void fetch_enums(const DirectoryLayout& layout, const URIConfig& uri_cfg);
    std::vector<std::string> fetch_enum_list(const DirectoryLayout& layout, const URIConfig& uri_cfg);
    void fetch_enum(const DirectoryLayout& layout,
                const URIConfig& uri_cfg,
                const std::string& enum_name,
                std::map<std::string, std::vector<std::string>>& enum_merged);

}