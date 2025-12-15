#include "../include/fetch_post.h"

#include <curl/curl.h>
#include <chrono>
#include <stdexcept>
#include <utility>

#include "../include/curl_common.h"

namespace metais {

    PostResult fetch_post(const PostFetchingSpec& spec, const HTTPConfig& http_cfg) {
        PostResult res;

        const std::string payload_str = spec.payload.dump();
        auto t0 = std::chrono::steady_clock::now();

        CURL* curl = curl_easy_init();
        if (!curl) throw std::runtime_error("curl_easy_init() failed");

        std::string buffer;

        curl_easy_setopt(curl, CURLOPT_URL, spec.api_url.c_str());
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, payload_str.c_str());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)payload_str.size());

        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_string_cb);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buffer);

        curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, spec.follow_redirects ? 1L : 0L);
        curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, http_cfg.timeouts.connect_seconds);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT, http_cfg.timeouts.total_seconds);
        curl_easy_setopt(curl, CURLOPT_USERAGENT, "metais-cpp-fetcher/1.0");

        CurlSlist headers;
        headers.add("Accept: application/json");
        headers.add("Content-Type: application/json");
        if (!spec.auth_header.empty()) headers.add(spec.auth_header);

        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers.get());

        CURLcode rc = curl_easy_perform(curl);

        long http_code = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);

        res.content_type = get_content_type(curl);

        curl_easy_cleanup(curl);

        auto t1 = std::chrono::steady_clock::now();
        res.seconds = std::chrono::duration<double>(t1 - t0).count();

        res.raw_body = std::move(buffer);

        if (rc != CURLE_OK) {
            res.transport_ok = false;
            res.curl_code = (int)rc;
            res.http_code = 0;
            res.parse_ok = false;
            return res;
        }

        res.transport_ok = true;
        res.curl_code = 0;
        res.http_code = http_code;

        if (spec.parse_json) {
            try {
                res.body = json::parse(res.raw_body);
                res.parse_ok = true;
            } catch (const std::exception& e) {
                res.parse_ok = false;
                res.parse_error = e.what();
            }
        }

        return res;
    }

}