#pragma once

#include <string>
#include <filesystem>

#include "directory_layout.h"
#include "page_sink.h"
#include "URI_config.h"
#include "http_config.h"
#include "http_response.h"

namespace metais {

    void fetch_raw_nodes(
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        PageSink& sink
    );

    void fetch_raw_rels(
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        PageSink& sink
    );

}