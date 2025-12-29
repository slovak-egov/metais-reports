#pragma once

#include "directory_layout.h"
#include "URI_config.h"
#include "http_config.h"

namespace metais {

    void fetch_codelists(const DirectoryLayout& layout,
                         const URIConfig& uri_cfg,
                         const HTTPConfig& http_cfg);
                         
}