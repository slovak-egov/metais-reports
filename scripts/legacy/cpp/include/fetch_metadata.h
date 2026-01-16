#pragma once

#include <string>
#include <vector>
#include <map>

#include "directory_layout.h"
#include "URI_config.h"
#include "http_config.h"

namespace metais {

    // Top-level orchestrator: fetch citype + reltype metadata,
    // honoring .done markers in nodes_meta_dir / rels_meta_dir.
    void fetch_metadata(const DirectoryLayout& layout,
                        const URIConfig& uri_cfg,
                        const HTTPConfig& http_cfg);

}