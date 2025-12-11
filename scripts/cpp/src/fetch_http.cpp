#include "../include/fetch_http.h"
#include <curl/curl.h>
#include <stdexcept>
#include <iostream>

using json = nlohmann::json;

namespace {

    // callback for curl
    size_t write_string_cb(void* contents, size_t size, size_t nmemb, void* userp) {
        size_t total = size * nmemb;
        auto* s = static_cast<std::string*>(userp);
        s->append(static_cast<char*>(contents), total);
        return total;
    }

} // anonymous namespace

namespace http {

    std::string GET(const std::string& url, const std::string& bearer_token) {
        CURL* curl = curl_easy_init();
        if (!curl) {
            throw std::runtime_error("curl_easy_init() failed");
        }

        std::string buffer;

        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_string_cb);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buffer);
        curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT, 60L);
        curl_easy_setopt(curl, CURLOPT_USERAGENT, "metais-cpp-fetcher/1.0");

        // Headers
        struct curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, "Accept: application/json");

        std::string auth_header;
        if (!bearer_token.empty()) {
            auth_header = "Authorization: Bearer " + bearer_token;
            headers = curl_slist_append(headers, auth_header.c_str());
        }

        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);

        // Perform request
        CURLcode rc = curl_easy_perform(curl);

        long http_code = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);

        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);

        if (rc != CURLE_OK) {
            throw std::runtime_error(
                std::string("curl_easy_perform() failed: ") +
                curl_easy_strerror(rc)
            );
        }

        if (http_code < 200 || http_code >= 300) {
            throw std::runtime_error(
                "HTTP GET " + url +
                " returned status " + std::to_string(http_code));
        }

        return buffer;
    }

    json GET_json(const std::string& url, const std::string& bearer_token) {
        std::string body = GET(url, bearer_token);

        try {
            return json::parse(body);
        }
        catch (const std::exception& e) {
            throw std::runtime_error(
                "Failed to parse JSON from " + url + ": " + std::string(e.what())
            );
        }
    }

}