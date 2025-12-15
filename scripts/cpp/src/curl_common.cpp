#include "../include/curl_common.h"

namespace metais {

    size_t write_string_cb(void* contents, size_t size, size_t nmemb, void* userp) {
        const size_t n = size * nmemb;
        auto* s = static_cast<std::string*>(userp);
        s->append(static_cast<const char*>(contents), n);
        return n;
    }

    std::string get_content_type(CURL* curl) {
        char* ct = nullptr;
        if (curl_easy_getinfo(curl, CURLINFO_CONTENT_TYPE, &ct) == CURLE_OK && ct) {
            return std::string(ct);
        }
        return {};
    }

}