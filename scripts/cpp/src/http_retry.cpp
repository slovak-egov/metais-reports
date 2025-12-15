#include "../include/http_retry.h"

#include <algorithm>
#include <random>

namespace metais {
    
    bool should_retry_curl(CURLcode rc, const HTTPConfig& cfg) {
        for (const auto& s : cfg.retries.retry_curl) {
            if (s == "timeout" && rc == CURLE_OPERATION_TIMEDOUT) return true;
            if (s == "couldnt_connect" && rc == CURLE_COULDNT_CONNECT) return true;
            if (s == "couldnt_resolve_host" && rc == CURLE_COULDNT_RESOLVE_HOST) return true;
        }
        return false;
    }

    static bool contains_status(const std::vector<long>& v, long code) {
        return std::find(v.begin(), v.end(), code) != v.end();
    }

    bool should_retry_http(long status, const HTTPConfig& cfg) {
        return contains_status(cfg.retries.retry_http, status);
    }
    
    int compute_backoff_ms(const metais::HTTPRetriesConfig& r, int attempt_index_0based) {
        // exponential backoff: base * 2^attempt
        long long delay = (long long)r.base_delay_ms * (1LL << attempt_index_0based);
        if (delay > r.max_delay_ms) delay = r.max_delay_ms;

        // jitter
        if (r.jitter_ms > 0) {
            static thread_local std::mt19937 rng{std::random_device{}()};
            std::uniform_int_distribution<int> dist(0, r.jitter_ms);
            delay += dist(rng);
        }
        return (int)delay;
    }
}