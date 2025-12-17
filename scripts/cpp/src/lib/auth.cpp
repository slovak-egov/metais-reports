#include "auth.h"

namespace metais {
    
    AuthDecision handle_auth_challenge(
        const HTTPConfig& cfg,
        const HttpResponse& r,
        std::string& bearer_token,
        bool interactive_allowed
    ) {
        if (!(r.status == 401 || r.status == 403)) return AuthDecision::Ignore;

        // If auth disabled, auth failures are unexpected: fail hard
        if (cfg.auth.mode == "none") return AuthDecision::FailHard;

        // 403 often means "token valid but insufficient rights".
        // Retrying the same identity usually won't help.
        // If you later add a "refresh via client credentials", you can branch here.
        if (r.status == 403) {
            // You *may* still want to allow interactive override in dev,
            // but default should be fail in non-interactive contexts.
            if (!interactive_allowed) return AuthDecision::FailHard;
        }

        // First attempt: re-resolve non-interactively (env/file) in case token rotated
        std::string tok = resolve_bearer_token_noninteractive(cfg);
        if (!tok.empty() && tok != bearer_token) {
            bearer_token = tok;
            return AuthDecision::Retry;
        }

        // Interactive fallback
        if (interactive_allowed) {
            tok = prompt_bearer_token();
            if (!tok.empty() && tok != bearer_token) {
                bearer_token = tok;
                return AuthDecision::Retry;
            }
        }

        return AuthDecision::FailHard;
    }

    std::string read_file_trim(const std::filesystem::path& p) {
        std::ifstream in(p);
        if (!in.is_open()) return "";
        std::ostringstream ss;
        ss << in.rdbuf();
        std::string s = ss.str();
        while (!s.empty() && (s.back()=='\n' || s.back()=='\r' || s.back()==' ' || s.back()=='\t')) s.pop_back();
        while (!s.empty() && (s.front()==' ' || s.front()=='\t')) s.erase(s.begin());
        return s;
    }

    // Non-interactive: env -> file -> "" (if not required) or throw (if required)
    std::string resolve_bearer_token_noninteractive(const HTTPConfig& cfg) {
        const auto& a = cfg.auth;
        if (a.mode == "none") return "";

        // 1) env
        if (!a.env_var.empty()) {
            if (const char* v = std::getenv(a.env_var.c_str())) {
                std::string tok = v;
                if (!tok.empty()) return tok;
            }
        }

        // 2) file
        if (!a.token_file.empty()) {
            std::string tok = read_file_trim(a.token_file);
            if (!tok.empty()) return tok;
        }

        return "";
    }

    // Interactive fallback ONLY when needed (e.g. after 401/403)
    std::string prompt_bearer_token() {
        std::string tok;
        std::cerr << "[auth] Bearer token invalid/missing. Paste a token and press Enter:\n> ";
        std::getline(std::cin, tok);
        // trim
        while (!tok.empty() && (tok.back()=='\n' || tok.back()=='\r' || tok.back()==' ' || tok.back()=='\t')) tok.pop_back();
        while (!tok.empty() && (tok.front()==' ' || tok.front()=='\t')) tok.erase(tok.begin());
        return tok;
    }

}