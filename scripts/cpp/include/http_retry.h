#pragma once

#include "../include/http_config.h"

#include <curl/curl.h>

namespace metais {
    
    bool should_retry_curl(CURLcode rc, const HTTPConfig& cfg);
    bool should_retry_http(long status, const HTTPConfig& cfg);
    int compute_backoff_ms(const metais::HTTPRetriesConfig& r, int attempt_index_0based);

}