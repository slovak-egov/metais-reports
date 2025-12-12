#pragma once
#include <string>
#include <vector>
#include <filesystem>

namespace metais {

    struct HttpAuthSettings {
        std::string mode = "none";   // none | bearer_env | bearer_file | client_credentials (future)
        std::string env_var = "METAIS_TOKEN";
        std::string token_prefix = "Bearer ";
        std::string token_file = "";            // if mode=bearer_file
        bool required = false;
    };

    struct TimeoutSettings {
        long connect_seconds = 10;
        long total_seconds   = 60;
    };

    struct RetrySettings {
        int max_attempts = 5;
        int base_delay_ms = 500;
        int max_delay_ms  = 8000;
        int jitter_ms     = 250;

        std::vector<long> retry_http = {408, 429, 500, 502, 503, 504};
        std::vector<std::string> retry_curl = {"timeout", "couldnt_connect", "couldnt_resolve_host"};
    };

    struct PagingSettings {
        bool enabled = true;
        int page_size = 2000;
        int max_pages = 100000;
        std::string offset_param = "offset";
        std::string limit_param  = "limit";
    };

    struct HTTPConfig {
        HttpAuthSettings auth;
        TimeoutSettings timeouts;
        RetrySettings retries;
        PagingSettings paging;
    };

    HTTPConfig load_http_settings(const std::filesystem::path& json_path);

}