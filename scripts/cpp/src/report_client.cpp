#include "../include/report_client.h"
#include "../include/fetch_http.h"
#include "../include/json_utils.h"
#include <curl/curl.h>
#include <thread>
#include <random>
#include <stdexcept>
#include <iostream>

using json = nlohmann::json;

namespace {

size_t write_string_cb(void* contents, size_t size, size_t nmemb, void* userp) {
    size_t total = size * nmemb;
    auto* s = static_cast<std::string*>(userp);
    s->append(static_cast<char*>(contents), total);
    return total;
}

bool contains_status(const std::vector<long>& v, long code) {
    for (auto x : v) if (x == code) return true;
    return false;
}

int compute_backoff_ms(const metais::HTTPConfig& cfg, int attempt) {
    const auto& r = cfg.retries;
    int delay = r.base_delay_ms * (1 << std::min(attempt, 10));
    if (delay > r.max_delay_ms) delay = r.max_delay_ms;

    std::mt19937 rng{std::random_device{}()};
    std::uniform_int_distribution<int> jitter(0, r.jitter_ms);
    delay += jitter(rng);
    return delay;
}

} // namespace

namespace metais {

    static bool should_retry_curl(CURLcode rc, const HTTPConfig& cfg) {
        for (const auto& s : cfg.retries.retry_curl) {
            if (s == "timeout" && rc == CURLE_OPERATION_TIMEDOUT) return true;
            if (s == "couldnt_connect" && rc == CURLE_COULDNT_CONNECT) return true;
            if (s == "couldnt_resolve_host" && rc == CURLE_COULDNT_RESOLVE_HOST) return true;
        }
        return false;
    }
    
    bool should_retry_http(long status, const HTTPConfig& cfg) {
        return contains_status(cfg.retries.retry_http, status);
    }

    static std::string resolve_bearer_token_required(const std::string& token) {
        if (token.empty()) throw std::runtime_error("Bearer token required for report POST but empty");
        return token;
    }

    HttpResponse run_report_groovy(
        const ReportRunOptions& opt,
        const HTTPConfig& http_cfg,
        const std::string& groovy_code
    ) {
        const std::string token = resolve_bearer_token_required(opt.bearer_token);

        // params.json is NOT the full payload. It's the "parameters" object.
        json params = load_json_file("config/params/params.json");
        if (!params.is_object()) {
            throw std::runtime_error("params.json must be a JSON object (for payload.parameters)");
        }

        json payload;
        payload["body"]       = groovy_code;
        payload["parameters"] = params;

        const std::string payload_str = payload.dump();

        for (int attempt = 0; attempt < http_cfg.retries.max_attempts; ++attempt) {
            auto t0 = std::chrono::steady_clock::now();

            CURL* curl = curl_easy_init();
            if (!curl) throw std::runtime_error("curl_easy_init() failed");

            std::string buffer;

            curl_easy_setopt(curl, CURLOPT_URL, opt.api_url.c_str());
            curl_easy_setopt(curl, CURLOPT_POST, 1L);
            curl_easy_setopt(curl, CURLOPT_POSTFIELDS, payload_str.c_str());
            curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)payload_str.size());

            curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_string_cb);
            curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buffer);

            curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
            curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, http_cfg.timeouts.connect_seconds);
            curl_easy_setopt(curl, CURLOPT_TIMEOUT, http_cfg.timeouts.total_seconds);
            curl_easy_setopt(curl, CURLOPT_USERAGENT, "metais-cpp-fetcher/1.0");

            struct curl_slist* headers = nullptr;
            headers = curl_slist_append(headers, "Accept: application/json");
            headers = curl_slist_append(headers, "Content-Type: application/json");

            std::string auth = "Authorization: Bearer " + token;
            headers = curl_slist_append(headers, auth.c_str());
            curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);

            CURLcode rc = curl_easy_perform(curl);

            long http_code = 0;
            curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);

            curl_slist_free_all(headers);
            curl_easy_cleanup(curl);

            auto t1 = std::chrono::steady_clock::now();
            double seconds = std::chrono::duration<double>(t1 - t0).count();

            // curl-level failures (timeouts, DNS, connect, etc.)
            if (rc != CURLE_OK) {
                if (should_retry_curl(rc, http_cfg) && attempt + 1 < http_cfg.retries.max_attempts) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(compute_backoff_ms(http_cfg, attempt)));
                    continue;
                }
                HttpResponse r;
                r.status = 0;
                r.curl_code = (int)rc;
                r.body = curl_easy_strerror(rc);
                r.seconds = seconds;
                return r;
            }

            // HTTP retryable
            if (http_code < 200 || http_code >= 300) {
                if (should_retry_http(http_code, http_cfg) && attempt + 1 < http_cfg.retries.max_attempts) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(compute_backoff_ms(http_cfg, attempt)));
                    continue;
                }
                HttpResponse r;
                r.status = http_code;
                r.body = std::move(buffer);
                r.seconds = seconds;
                return r;
            }

            // OK
            HttpResponse ok;
            ok.status = http_code;
            ok.body = std::move(buffer);
            ok.seconds = seconds;
            return ok;
        }

        throw std::runtime_error("Unreachable: report retry loop fell through");
    }

}