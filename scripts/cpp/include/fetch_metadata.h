#pragma once

#include <string>
#include <vector>
#include <map>

#include "directory_layout.h"
#include "URI_config.h"
#include "http_config.h"

namespace metais {

    // Fetch citype list JSON and return list of technicalNames.
    std::vector<std::string>
    fetch_citype_list(const DirectoryLayout& layout,
                      const URIConfig& uri_cfg,
                      const HTTPConfig& http_cfg);

    // Fetch one citype detail and store it under nodes_meta_dir/<code>.json
    void fetch_citype_detail(const DirectoryLayout& layout,
                             const URIConfig& uri_cfg,
                             const HTTPConfig& http_cfg,
                             const std::string& citype_code);

    // Fetch reltype list JSON and return list of technicalNames.
    std::vector<std::string>
    fetch_reltype_list(const DirectoryLayout& layout,
                       const URIConfig& uri_cfg,
                       const HTTPConfig& http_cfg);

    // Fetch one reltype detail and store it under rels_meta_dir/<code>.json
    void fetch_reltype_detail(const DirectoryLayout& layout,
                              const URIConfig& uri_cfg,
                              const HTTPConfig& http_cfg,
                              const std::string& reltype_code);

    // Top-level orchestrator: fetch citype + reltype metadata,
    // honoring .done markers in nodes_meta_dir / rels_meta_dir.
    void fetch_metadata(const DirectoryLayout& layout,
                        const URIConfig& uri_cfg,
                        const HTTPConfig& http_cfg);

}