#include "http_config.h"
#include "json_utils.h"

#include <nlohmann/json.hpp>
#include <stdexcept>

using json = nlohmann::json;

namespace metais {

    static void load_auth(const json& j, HTTPAuthConfig& a) {
        if (!j.is_object()) return;
        if (j.contains("mode"))         a.mode         = j["mode"].get<std::string>();
        if (j.contains("env_var"))      a.env_var      = j["env_var"].get<std::string>();
        if (j.contains("token_prefix")) a.token_prefix = j["token_prefix"].get<std::string>();
        if (j.contains("required"))     a.required     = j["required"].get<bool>();
        if (j.contains("token_file"))   a.token_file   = j["token_file"].get<std::string>();

        if (j.contains("user_env"))       a.user_env = j["user_env"].get<std::string>();
        if (j.contains("pass_env"))       a.pass_env = j["pass_env"].get<std::string>();
        if (j.contains("interactive"))    a.interactive = j["interactive"].get<bool>();

        if (j.contains("client_id"))      a.client_id = j["client_id"].get<std::string>();
        if (j.contains("redirect_path"))  a.redirect_path = j["redirect_path"].get<std::string>();
        if (j.contains("scope"))          a.scope = j["scope"].get<std::string>();

        if (j.contains("authorize_path")) a.authorize_path = j["authorize_path"].get<std::string>();
        if (j.contains("token_path"))     a.token_path     = j["token_path"].get<std::string>();
        if (j.contains("login_path"))     a.login_path     = j["login_path"].get<std::string>();

        if (j.contains("user_agent"))     a.user_agent = j["user_agent"].get<std::string>();
    }

    static void load_timeouts(const json& j, HTTPTimeoutsConfig& t) {
        if (!j.is_object()) return;
        t.connect_seconds = j.value("connect_seconds", t.connect_seconds);
        t.total_seconds   = j.value("total_seconds",   t.total_seconds);
    }

    static void load_retries(const json& j, HTTPRetriesConfig& r) {
        if (!j.is_object()) return;

        r.max_attempts  = j.value("max_attempts",  r.max_attempts);
        r.base_delay_ms = j.value("base_delay_ms", r.base_delay_ms);
        r.max_delay_ms  = j.value("max_delay_ms",  r.max_delay_ms);
        r.jitter_ms     = j.value("jitter_ms",     r.jitter_ms);

        if (j.contains("retry_http") && j["retry_http"].is_array()) {
            r.retry_http.clear();
            for (const auto& x : j["retry_http"]) {
                if (x.is_number_integer()) r.retry_http.push_back(x.get<long>());
            }
        }

        if (j.contains("retry_curl") && j["retry_curl"].is_array()) {
            r.retry_curl.clear();
            for (const auto& x : j["retry_curl"]) {
                if (x.is_string()) r.retry_curl.push_back(x.get<std::string>());
            }
        }
    }

    static void load_paging(const json& j, HTTPPagingConfig& p) {
        if (!j.is_object()) return;

        p.enabled      = j.value("enabled",      p.enabled);
        p.page_size    = j.value("page_size",    p.page_size);
        p.max_pages    = j.value("max_pages",    p.max_pages);
        p.offset_param = j.value("offset_param", p.offset_param);
        p.limit_param  = j.value("limit_param",  p.limit_param);
    }

    HTTPConfig load_http_settings(const std::string& path) {
        HTTPConfig s;
        json j = load_json_file(path);

        if (!j.is_object()) {
            throw std::runtime_error("http_config must be a JSON object: " + path);
        }

        load_auth    (j.value("auth",     json::object()), s.auth);
        load_timeouts(j.value("timeouts", json::object()), s.timeouts);
        load_retries (j.value("retries",  json::object()), s.retries);
        load_paging  (j.value("paging",   json::object()), s.paging);

        return s;
    }

}