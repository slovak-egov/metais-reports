#include "auth.h"

#include <termios.h>
#include <unistd.h>

namespace metais {

    static std::string g_cached_user;
    static std::string g_cached_pass;

    static std::string read_password_noecho() {
        termios oldt{}, newt{};
        tcgetattr(STDIN_FILENO, &oldt);
        newt = oldt;
        newt.c_lflag &= ~ECHO;

        tcsetattr(STDIN_FILENO, TCSANOW, &newt);

        std::string pass;
        std::getline(std::cin, pass);

        tcsetattr(STDIN_FILENO, TCSANOW, &oldt);
        std::cerr << "\n";

        return pass;
    }
    
    static void ensure_userpass(
        const HTTPAuthConfig& a,
        bool interactive_allowed
    ) {
        if (!g_cached_user.empty() && !g_cached_pass.empty()) return;

        if (const char* u = std::getenv(a.user_env.c_str())) if (*u) g_cached_user = u;
        if (const char* p = std::getenv(a.pass_env.c_str())) if (*p) g_cached_pass = p;

        if ((!g_cached_user.empty() && !g_cached_pass.empty())) return;

        if (!a.interactive || !interactive_allowed) return;

        std::cerr << "[auth] Username: ";
        std::getline(std::cin, g_cached_user);
        std::cerr << "[auth] Password: ";
        g_cached_pass = read_password_noecho();
    }

    static std::string obtain_token_oidc(
        const HTTPConfig& cfg,
        bool interactive_allowed,
        const std::string& base_url
    ) {
        const auto& a = cfg.auth;

        ensure_userpass(a, interactive_allowed);

        if (g_cached_user.empty() || g_cached_pass.empty()) {
            return ""; // caller decides if this is fatal
        }

        const std::string redirect_uri = base_url + a.redirect_path;

        auto res = oidc_login_userpass_pkce(
            base_url,
            a.authorize_path,
            a.login_path,
            a.token_path,
            a.client_id,
            redirect_uri,
            a.scope,
            g_cached_user,
            g_cached_pass,
            a.user_agent
        );

        return res.access_token;
    }

    AuthDecision handle_auth_challenge(
        const HTTPConfig& cfg,
        const HttpResponse& r,
        std::string& bearer_token,
        bool interactive_allowed,
        const std::string& base_url
    ) {
        if (r.status != 401 && r.status != 403) return AuthDecision::Ignore;

        if (cfg.auth.mode == "none") return AuthDecision::FailHard;

        // 1) Re-resolve (env/file OR OIDC refresh depending on mode).
        //    Note: resolve_* may prompt for user/pass if your config allows it and interactive_allowed=true.
        std::string tok = resolve_bearer_token_noninteractive(cfg, interactive_allowed, base_url);
        if (!tok.empty() && tok != bearer_token) {
            bearer_token = std::move(tok);
            std::cerr << "[auth] Token refreshed; retrying request.\n";
            return AuthDecision::Retry;
        }

        // 2) Legacy manual token prompt fallback (ONLY for bearer-style modes).
        //    (In OIDC mode, you typically do NOT want “paste a token” as a fallback.)
        if (interactive_allowed && cfg.auth.mode != "oidc_userpass_pkce") {
            tok = prompt_bearer_token();
            if (!tok.empty() && tok != bearer_token) {
                bearer_token = std::move(tok);
                std::cerr << "[auth] Token updated manually; retrying request.\n";
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

    std::string resolve_bearer_token_noninteractive(
        const HTTPConfig& cfg,
        bool interactive_allowed,
        const std::string& base_url
    ) {
        const auto& a = cfg.auth;
        if (a.mode == "none") return "";

        // --- OIDC mode: derive fresh token using username/password ---
        if (a.mode == "oidc_userpass_pkce") {
            return obtain_token_oidc(cfg, interactive_allowed, base_url);
        }

        // --- Legacy bearer token mode: env -> file -> "" ---
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