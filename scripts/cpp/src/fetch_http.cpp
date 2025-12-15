#include "../include/fetch_http.h"
#include "../include/http_config.h"
#include "../include/http_retry.h"
#include <curl/curl.h>

#include <cstdlib>
#include <chrono>
#include <thread>
#include <random>
#include <iostream>
#include <algorithm>

using json = nlohmann::json;

namespace {

    // callback for curl body
    size_t write_string_cb(void* contents, size_t size, size_t nmemb, void* userp) {
        size_t total = size * nmemb;
        auto* s = static_cast<std::string*>(userp);
        s->append(static_cast<char*>(contents), total);
        return total;
    }

    bool contains_status(const std::vector<long>& v, long code) {
        return std::find(v.begin(), v.end(), code) != v.end();
    }

    // Map CURLcode to configured names
    std::string curl_code_to_key(CURLcode rc) {
        switch (rc) {
            case CURLE_OPERATION_TIMEDOUT:    return "timeout";
            case CURLE_COULDNT_CONNECT:       return "couldnt_connect";
            case CURLE_COULDNT_RESOLVE_HOST:  return "couldnt_resolve_host";
            case CURLE_COULDNT_RESOLVE_PROXY: return "couldnt_resolve_proxy";
            case CURLE_SEND_ERROR:            return "send_error";
            case CURLE_RECV_ERROR:            return "recv_error";
            default:                          return "other";
        }
    }

    bool contains_string(const std::vector<std::string>& v, const std::string& s) {
        return std::find(v.begin(), v.end(), s) != v.end();
    }

    std::string resolve_bearer_token(const metais::HTTPConfig& settings) {
        const auto& a = settings.auth;

        if (a.mode == "none") return "";

        if (a.mode == "bearer_env") {
            const char* v = std::getenv(a.env_var.c_str());
            if (!v || std::string(v).empty()) {
                if (a.required) {
                    throw std::runtime_error(
                        "Auth required (bearer_env) but env var '" + a.env_var + "' is not set"
                    );
                }
                // optional auth: proceed without token
                return "";
            }
            return std::string(v);
        }

        // future modes can go here
        throw std::runtime_error("Unknown auth.mode: " + a.mode);
    }

}

namespace http {

    // -------------------------
    // LOW-LEVEL GET (no retries)
    // -------------------------
    std::string GET(const std::string& url, const std::string& bearer_token) {
        CURL* curl = curl_easy_init();
        if (!curl) throw std::runtime_error("curl_easy_init() failed");

        std::string buffer;

        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_string_cb);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buffer);
        curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT, 60L); // default if caller uses this overload
        curl_easy_setopt(curl, CURLOPT_USERAGENT, "metais-cpp-fetcher/1.0");

        struct curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, "Accept: application/json");

        if (!bearer_token.empty()) {
            std::string auth_header = "Authorization: Bearer " + bearer_token;
            headers = curl_slist_append(headers, auth_header.c_str());
        }

        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);

        CURLcode rc = curl_easy_perform(curl);

        long http_code = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);

        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);

        if (rc != CURLE_OK) {
            throw std::runtime_error(
                std::string("curl_easy_perform() failed: ") + curl_easy_strerror(rc)
            );
        }

        if (http_code < 200 || http_code >= 300) {
            throw HttpError(http_code, url, buffer);
        }

        return buffer;
    }

    json GET_json(const std::string& url, const std::string& bearer_token) {
        std::string body = GET(url, bearer_token);
        try {
            return json::parse(body);
        } catch (const std::exception& e) {
            throw std::runtime_error(
                "Failed to parse JSON from " + url + ": " + std::string(e.what())
            );
        }
    }

    // ----------------------------------------
    // HIGH-LEVEL GET (settings: retries/backoff)
    // ----------------------------------------
    std::string GET(const std::string& url, const metais::HTTPConfig& settings) {
        const std::string token = resolve_bearer_token(settings);

        for (int attempt = 1; attempt <= settings.retries.max_attempts; ++attempt) {
            CURL* curl = curl_easy_init();
            if (!curl) throw std::runtime_error("curl_easy_init() failed");

            std::string buffer;

            curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
            curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_string_cb);
            curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buffer);
            curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);

            curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, settings.timeouts.connect_seconds);
            curl_easy_setopt(curl, CURLOPT_TIMEOUT, settings.timeouts.total_seconds);

            curl_easy_setopt(curl, CURLOPT_USERAGENT, "metais-cpp-fetcher/1.0");

            struct curl_slist* headers = nullptr;
            headers = curl_slist_append(headers, "Accept: application/json");

            if (!token.empty()) {
                std::string auth_header = settings.auth.token_prefix + token;
                // ensure prefix ends with space? your JSON uses "Bearer " already.
                headers = curl_slist_append(headers, ("Authorization: " + auth_header).c_str());
            }

            curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);

            CURLcode rc = curl_easy_perform(curl);

            long http_code = 0;
            curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);

            curl_slist_free_all(headers);
            curl_easy_cleanup(curl);

            // 1) CURL/network failures: maybe retry depending on config
            if (rc != CURLE_OK) {
                const std::string key = curl_code_to_key(rc);
                const bool retryable = contains_string(settings.retries.retry_curl, key);

                if (!retryable || attempt == settings.retries.max_attempts) {
                    throw std::runtime_error(
                        std::string("curl failed (") + key + "): " + curl_easy_strerror(rc)
                    );
                }

                const int delay = metais::compute_backoff_ms(settings.retries, attempt - 1);
                std::cerr << "[HTTP] curl error (" << key << ") attempt " << attempt
                        << "/" << settings.retries.max_attempts
                        << " -> retry in " << delay << " ms\n";
                std::this_thread::sleep_for(std::chrono::milliseconds(delay));
                continue;
            }

            // 2) HTTP codes: success
            if (http_code >= 200 && http_code < 300) {
                return buffer;
            }

            // 3) HTTP codes: decide retry strategy
            const bool retryable_http = contains_status(settings.retries.retry_http, http_code);

            // Generally: do NOT retry auth failures automatically in this mode.
            // (Later you can add token refresh for client_credentials mode.)
            if (!retryable_http || attempt == settings.retries.max_attempts) {
                throw HttpError(http_code, url, buffer);
            }

            const int delay = metais::compute_backoff_ms(settings.retries, attempt - 1);
            std::cerr << "[HTTP] HTTP " << http_code << " attempt " << attempt
                    << "/" << settings.retries.max_attempts
                    << " -> retry in " << delay << " ms\n";
            std::this_thread::sleep_for(std::chrono::milliseconds(delay));
        }

        // Should never get here
        throw std::runtime_error("GET retry loop exhausted unexpectedly for " + url);
    }

    json GET_json(const std::string& url, const metais::HTTPConfig& settings) {
        std::string body = GET(url, settings);
        try {
            return json::parse(body);
        } catch (const std::exception& e) {
            throw std::runtime_error(
                "Failed to parse JSON from " + url + ": " + std::string(e.what())
            );
        }
    }

}