#pragma once

#include "http_config.h"

#include <nlohmann/json.hpp>

#include <string>

namespace metais {

    using json = nlohmann::json;

    struct PostFetchingSpec {
        std::string tag;
        std::string label;

        std::string api_url;
        json payload;

        bool parse_json = true;
        bool follow_redirects = true;

        // auth header content (caller provides token / API key / whatever)
        // Example: "Authorization: Bearer XXX"
        std::string auth_header;
    };

    struct PostResult {
        bool transport_ok = false;
        int  curl_code = 0;
        long http_code = 0;

        json body;
        std::string raw_body;
        std::string parse_error;
        bool parse_ok = false;

        double seconds = 0.0;
        std::string content_type;
    };

    // make a POST request
    PostResult fetch_post(const PostFetchingSpec& spec, const HTTPConfig& http_cfg);
}