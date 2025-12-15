#pragma once

#include <curl/curl.h>
#include <chrono>
#include <stdexcept>
#include <utility>
#include <string>

namespace metais {

    struct CurlSlist {
        curl_slist* p = nullptr;
        ~CurlSlist() { if (p) curl_slist_free_all(p); }
        void add(const std::string& s) { p = curl_slist_append(p, s.c_str()); }
        curl_slist* get() const { return p; }
    };

    size_t write_string_cb(void* contents, size_t size, size_t nmemb, void* userp);
    std::string get_content_type(CURL* curl);

}