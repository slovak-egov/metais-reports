#pragma once

#include <string>

namespace metais {

    struct HttpResponse {
        long status    = 0;
        int  curl_code = 0;
        std::string body;
        double seconds = 0.0;
    };

    struct ReportRunOptions {
        std::string api_url;
        std::string bearer_token;
        int  limit  = 0;
        long offset = 0;
    };

}