#pragma once

#include <string>
#include <chrono>
#include <nlohmann/json.hpp>
#include "http_config.h"

namespace metais {

    struct HttpResponse {
        long status = 0;
        int  curl_code = 0;
        std::string body;
        double seconds = 0.0;
    };

    struct ReportRunOptions {
        std::string api_url;
        std::string bearer_token;
        int limit  = 1000;
        long offset = 0;

        std::string dateFrom = "";
        std::string dateTo   = "";
        std::string ico      = "";
        std::string state    = "";
    };

    HttpResponse run_report_groovy(
        const ReportRunOptions& opt,
        const HTTPConfig& http_cfg,
        const std::string& groovy_code
    );

    bool should_retry_http(long status, const HTTPConfig& cfg);
}