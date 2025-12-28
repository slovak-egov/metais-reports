#pragma once
#include <string>
#include <vector>

namespace metais {

    struct HTTPAuthConfig {
        std::string mode;          // "none", "bearer_env", "oidc_userpass_pkce"
        std::string env_var;       // bearer token env var (for bearer_env)
        std::string token_prefix;
        bool required = true;
        std::string token_file;

        // username/password for oidc_userpass_pkce
        std::string user_env = "METAIS_USER";
        std::string pass_env = "METAIS_PASS";
        bool interactive = true;

        // OIDC knobs (host comes from URIConfig.base_url) 
        std::string client_id = "webPortalClient";
        std::string redirect_path = "/auth-success";         // not full URL
        std::string scope = "openid";

        // IAM paths are stable, but keep them configurable if you want:
        std::string authorize_path = "/iam/authorize";
        std::string token_path     = "/iam/token";
        std::string login_path     = "/iam/usernamePassLogin";

        std::string user_agent = "metais-cpp-fetcher/1.0";
    };

    struct HTTPTimeoutsConfig {
        int connect_seconds = 10;
        int total_seconds   = 60;
    };

    struct HTTPRetriesConfig {
        int max_attempts = 5;
        int base_delay_ms = 500;
        int max_delay_ms = 8000;
        int jitter_ms = 250;
        std::vector<long> retry_http;
        std::vector<std::string> retry_curl;
    };

    struct HTTPPagingConfig {
        bool enabled = true;
        int page_size = 1000;
        long max_pages = 100000;
        std::string offset_param = "offset";
        std::string limit_param  = "limit";
    };

    struct HTTPConfig {
        HTTPAuthConfig auth;
        HTTPTimeoutsConfig timeouts;
        HTTPRetriesConfig retries;
        HTTPPagingConfig paging;
    };

    HTTPConfig load_http_settings(const std::string& path);

}