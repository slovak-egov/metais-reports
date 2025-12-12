#include "../include/http_config.h"
#include "../include/json_utils.h"
#include <iostream>

using json = nlohmann::json;

namespace metais {

    static void load_auth(const json& j, HttpAuthSettings& a) {
        if (!j.is_object()) return;
        if (j.contains("mode"))
            a.mode = j.value("mode", a.mode);
        if (j.contains("env_var"))
            a.env_var = j.value("env_var", a.env_var);
        if (j.contains("token_prefix"))
            a.token_prefix = j.value("token_prefix", a.token_prefix);
        if (j.contains("token_file"))
            a.token_file = j.value("token_file", a.token_file);
        if (j.contains("required") && j["required"].is_boolean())
            a.required = j["required"].get<bool>();
    }

    static void load_timeouts(const json& j, TimeoutSettings& t) {
        if (!j.is_object()) return;
        t.connect_seconds = j.value("connect_seconds", t.connect_seconds);
        t.total_seconds   = j.value("total_seconds", t.total_seconds);
    }

    static void load_retries(const json& j, RetrySettings& r) {
        if (!j.is_object()) return;
        r.max_attempts = j.value("max_attempts", r.max_attempts);
        r.base_delay_ms= j.value("base_delay_ms", r.base_delay_ms);
        r.max_delay_ms = j.value("max_delay_ms", r.max_delay_ms);
        r.jitter_ms    = j.value("jitter_ms", r.jitter_ms);

        if (j.contains("retry_http") && j["retry_http"].is_array()) {
            r.retry_http.clear();
            for (auto& x : j["retry_http"]) if (x.is_number_integer()) r.retry_http.push_back(x.get<long>());
        }
        if (j.contains("retry_curl") && j["retry_curl"].is_array()) {
            r.retry_curl.clear();
            for (auto& x : j["retry_curl"]) if (x.is_string()) r.retry_curl.push_back(x.get<std::string>());
        }
    }

    static void load_paging(const json& j, PagingSettings& p) {
        if (!j.is_object()) return;
        p.enabled     = j.value("enabled", p.enabled);
        p.page_size   = j.value("page_size", p.page_size);
        p.max_pages   = j.value("max_pages", p.max_pages);
        p.offset_param= j.value("offset_param", p.offset_param);
        p.limit_param = j.value("limit_param", p.limit_param);
    }

    HTTPConfig load_http_settings(const std::filesystem::path& json_path) {
        HTTPConfig s; // defaults
        try {
            auto j = load_json_file(json_path.string());
            load_auth(j.value("auth", json::object()), s.auth);
            load_timeouts(j.value("timeouts", json::object()), s.timeouts);
            load_retries(j.value("retries", json::object()), s.retries);
            load_paging(j.value("paging", json::object()), s.paging);
        } catch (const std::exception& e) {
            std::cerr << "[http_settings] WARNING: " << e.what()
                    << " - using defaults.\n";
        }
        return s;
    }

}