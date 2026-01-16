#pragma once

#include "json_utils.h"
#include "fetch_http.h"
#include "http_config.h"

#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <optional>
#include <string>
#include <vector>
#include <stdexcept>

#include <nlohmann/json.hpp>

namespace metais {

    using json = nlohmann::json;
    namespace fs = std::filesystem;

    struct OpenFetchingSpec {
        // output
        fs::path out_dir;
        std::string out_filename;

        // endpoints
        std::string list_url;   // for list fetch
        std::string detail_url_tpl; // e.g. "https://.../something/{name}"

        // logging
        std::string tag;        // e.g. "ENUMS", "META", "ENUM"
        std::string kind;       // e.g. "Citype", "Reltype", "Enum"
        std::string label;      // e.g. "Citype list", "Enum list" (for list logs)

        bool strict_mkdir = true;
        bool warn_if_created = true;

        bool log_received = true;
        bool log_written  = true;

        // transforms
        std::function<json(const json&)> transform = [](const json& d){ return d; };
    };

    // Fetch list JSON, save it (pretty), and return extracted IDs (codes).
    std::vector<std::string> fetch_element_list(
        const OpenFetchingSpec& spec,
        const HTTPConfig& http_cfg,
        const std::function<std::optional<std::string>(const json&)>& extract_id
    );
    
    // Fetch detail JSON, transform, save to <out_dir>/<code>.json, return the payload
    json fetch_detail(
        const std::string& detail_api_code,
        const HTTPConfig& http_cfg,
        const OpenFetchingSpec& spec
    );

}