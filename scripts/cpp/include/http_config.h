#pragma once
#include <string>
#include <vector>

namespace metais {

    struct HTTPAuthConfig {
        std::string mode;
        std::string env_var;
        std::string token_prefix;
        bool required = true;
        std::string token_file;
    };

    struct HTTPTimeoutsConfig {
        int connect_seconds = 10;
        int total_seconds   = 60;
    };

    struct HTTPRetriesConfig {
        int max_attempts = 5;
        int base_delay_ms = 500;
        int max_delay_ms = 8000;
        int jitter_ms = 250;
        std::vector<long> retry_http;
        std::vector<std::string> retry_curl;
    };

    struct HTTPPagingConfig {
        std::string mode = "serial_adaptive";
        int parallel_workers = 1;
        bool enabled = true;
        int page_size = 1000;
        long max_pages = 100000;
        std::string offset_param = "offset";
        std::string limit_param  = "limit";
    };

    struct HTTPConfig {
        HTTPAuthConfig auth;
        HTTPTimeoutsConfig timeouts;
        HTTPRetriesConfig retries;
        HTTPPagingConfig paging;
    };

    HTTPConfig load_http_settings(const std::string& path);

}