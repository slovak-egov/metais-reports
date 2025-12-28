#include "oidc_login.h"

#include <curl/curl.h>
#include <openssl/evp.h>
#include <openssl/rand.h>

#include <nlohmann/json.hpp>

#include <stdexcept>
#include <string>
#include <vector>
#include <map>
#include <regex>
#include <sstream>
#include <iostream>

namespace metais {

using json = nlohmann::json;

namespace {

    // ---------------------------
    // Helpers: string / URL
    // ---------------------------
    static bool starts_with(const std::string& s, const std::string& prefix) {
        return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
    }

    static std::string join_url(const std::string& base, const std::string& path) {
        if (path.empty()) return base;
        if (starts_with(path, "http://") || starts_with(path, "https://")) return path;
        if (!base.empty() && base.back() == '/' && !path.empty() && path.front() == '/') {
            return base.substr(0, base.size() - 1) + path;
        }
        if (!base.empty() && base.back() != '/' && !path.empty() && path.front() != '/') {
            return base + "/" + path;
        }
        return base + path;
    }

    static std::string url_encode(CURL* curl, const std::string& s) {
        char* enc = curl_easy_escape(curl, s.c_str(), (int)s.size());
        if (!enc) throw std::runtime_error("curl_easy_escape failed");
        std::string out(enc);
        curl_free(enc);
        return out;
    }

    static std::string build_query(CURL* curl, const std::vector<std::pair<std::string,std::string>>& kv) {
        std::ostringstream q;
        bool first = true;
        for (const auto& [k,v] : kv) {
            if (!first) q << "&";
            first = false;
            q << url_encode(curl, k) << "=" << url_encode(curl, v);
        }
        return q.str();
    }

    // ---------------------------
    // Helpers: crypto
    // ---------------------------

    // Base64url without '=' padding.
    // We base64-encode bytes using EVP_EncodeBlock (standard base64),
    // then transform to URL-safe: '+'->'-', '/'->'_', strip '='.
    static std::string base64url_encode(const unsigned char* data, size_t len) {
        // base64 output length: 4 * ceil(len/3)
        const size_t out_len = 4 * ((len + 2) / 3);
        std::string b64(out_len, '\0');

        int written = EVP_EncodeBlock(
            reinterpret_cast<unsigned char*>(&b64[0]),
            data,
            (int)len
        );
        if (written < 0) throw std::runtime_error("EVP_EncodeBlock failed");
        b64.resize((size_t)written);

        // url-safe transform
        for (char& c : b64) {
            if (c == '+') c = '-';
            else if (c == '/') c = '_';
        }
        // strip padding '='
        while (!b64.empty() && b64.back() == '=') b64.pop_back();
        return b64;
    }

    static std::string random_urlsafe_token(size_t nbytes = 32) {
        std::vector<unsigned char> buf(nbytes);
        if (RAND_bytes(buf.data(), (int)buf.size()) != 1) {
            throw std::runtime_error("RAND_bytes failed");
        }
        return base64url_encode(buf.data(), buf.size());
    }

    static std::pair<std::string,std::string> make_pkce() {
        // verifier: 32 random bytes -> base64url string
        std::vector<unsigned char> rnd(32);
        if (RAND_bytes(rnd.data(), (int)rnd.size()) != 1) {
            throw std::runtime_error("RAND_bytes failed (pkce verifier)");
        }
        std::string verifier = base64url_encode(rnd.data(), rnd.size());

        // challenge: SHA256(verifier UTF-8 bytes) -> base64url
        unsigned char md[EVP_MAX_MD_SIZE];
        unsigned int md_len = 0;

        EVP_MD_CTX* ctx = EVP_MD_CTX_new();
        if (!ctx) throw std::runtime_error("EVP_MD_CTX_new failed");

        if (EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr) != 1 ||
            EVP_DigestUpdate(ctx, verifier.data(), verifier.size()) != 1 ||
            EVP_DigestFinal_ex(ctx, md, &md_len) != 1) {
            EVP_MD_CTX_free(ctx);
            throw std::runtime_error("EVP sha256 failed");
        }
        EVP_MD_CTX_free(ctx);

        std::string challenge = base64url_encode(md, md_len);
        return {verifier, challenge};
    }

    // ---------------------------
    // CURL “session” and response capture
    // ---------------------------
    struct HttpResp {
        long status = 0;
        std::string body;
        std::string effective_url;
        std::string location;            // Location header (if any)
        std::multimap<std::string,std::string> headers;
    };

    static size_t write_body_cb(char* ptr, size_t size, size_t nmemb, void* userdata) {
        auto* out = reinterpret_cast<std::string*>(userdata);
        out->append(ptr, size * nmemb);
        return size * nmemb;
    }

    static size_t header_cb(char* buffer, size_t size, size_t nitems, void* userdata) {
        auto* resp = reinterpret_cast<HttpResp*>(userdata);
        const size_t n = size * nitems;

        std::string line(buffer, n);

        // Store raw header lines as parsed key: value when possible
        auto pos = line.find(':');
        if (pos != std::string::npos) {
            std::string key = line.substr(0, pos);
            // trim
            while (!key.empty() && (key.back()==' ' || key.back()=='\t')) key.pop_back();

            std::string val = line.substr(pos + 1);
            // trim
            while (!val.empty() && (val.front()==' ' || val.front()=='\t')) val.erase(val.begin());
            while (!val.empty() && (val.back()=='\r' || val.back()=='\n')) val.pop_back();

            resp->headers.emplace(key, val);

            // Capture Location specifically
            if (key == "Location" || key == "location") {
                resp->location = val;
            }
        }
        return n;
    }

    // Minimal GET/POST using one CURL* handle, cookies remain in the handle.
    // follow_redirects can be toggled per call.
    static HttpResp curl_request(
        CURL* curl,
        const std::string& method,
        const std::string& url,
        const std::vector<std::string>& header_lines,
        const std::string& body,
        bool follow_redirects
    ) {
        HttpResp resp;
        resp.body.clear();
        resp.location.clear();
        resp.headers.clear();

        curl_easy_reset(curl);

        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, follow_redirects ? 1L : 0L);
        curl_easy_setopt(curl, CURLOPT_MAXREDIRS, 20L);

        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_body_cb);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &resp.body);

        curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, header_cb);
        curl_easy_setopt(curl, CURLOPT_HEADERDATA, &resp);

        // Keep cookies in-memory “cookie engine”
        curl_easy_setopt(curl, CURLOPT_COOKIEFILE, ""); // enables cookie engine, empty means no initial file

        // Method
        if (method == "GET") {
            curl_easy_setopt(curl, CURLOPT_HTTPGET, 1L);
        } else if (method == "POST") {
            curl_easy_setopt(curl, CURLOPT_POST, 1L);
            curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
            curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)body.size());
        } else {
            throw std::runtime_error("Unsupported method: " + method);
        }

        // Headers
        struct curl_slist* slist = nullptr;
        for (const auto& h : header_lines) {
            slist = curl_slist_append(slist, h.c_str());
        }
        if (slist) curl_easy_setopt(curl, CURLOPT_HTTPHEADER, slist);

        CURLcode rc = curl_easy_perform(curl);

        if (slist) curl_slist_free_all(slist);

        if (rc != CURLE_OK) {
            throw std::runtime_error(std::string("curl_easy_perform failed: ") + curl_easy_strerror(rc));
        }

        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &resp.status);

        char* eff = nullptr;
        curl_easy_getinfo(curl, CURLINFO_EFFECTIVE_URL, &eff);
        if (eff) resp.effective_url = eff;

        return resp;
    }

    static std::string extract_csrf(const std::string& html) {
        // match: <input type="hidden" name="_csrf" value="...">
        {
            std::regex re(R"(name=["']_csrf["'][^>]*value=["']([^"']+)["'])");
            std::smatch m;
            if (std::regex_search(html, m, re) && m.size() >= 2) return m[1].str();
        }
        // or meta tag: <meta name="_csrf" content="...">
        {
            std::regex re(R"(<meta[^>]*name=["']_csrf["'][^>]*content=["']([^"']+)["'])");
            std::smatch m;
            if (std::regex_search(html, m, re) && m.size() >= 2) return m[1].str();
        }
        throw std::runtime_error("Could not find CSRF token in HTML");
    }

    static std::map<std::string,std::string> parse_query(const std::string& url) {
        auto qpos = url.find('?');
        if (qpos == std::string::npos) return {};
        std::string q = url.substr(qpos + 1);

        std::map<std::string,std::string> out;
        std::stringstream ss(q);
        std::string part;
        while (std::getline(ss, part, '&')) {
            auto eq = part.find('=');
            if (eq == std::string::npos) continue;
            out[part.substr(0, eq)] = part.substr(eq + 1);
        }
        return out;
    }

    static std::string ensure_absolute_location(const std::string& base_url, const std::string& loc) {
        if (loc.empty()) return loc;
        if (starts_with(loc, "http://") || starts_with(loc, "https://")) return loc;
        if (!loc.empty() && loc.front() == '/') return base_url + loc;
        // relative without leading slash (rare here)
        return base_url + "/" + loc;
    }

    } // namespace

    OIDCLoginResult oidc_login_userpass_pkce(
        const std::string& base_url,
        const std::string& authorize_path,
        const std::string& login_path,
        const std::string& token_path,
        const std::string& client_id,
        const std::string& redirect_uri,
        const std::string& scope,
        const std::string& username,
        const std::string& password,
        const std::string& user_agent
    ) {
        curl_global_init(CURL_GLOBAL_DEFAULT);
        CURL* curl = curl_easy_init();
        if (!curl) throw std::runtime_error("curl_easy_init failed");

        // UA (like your Python session header)
        curl_easy_setopt(curl, CURLOPT_USERAGENT, user_agent.c_str());

        // --- 0) PKCE + state/nonce ---
        auto [code_verifier, code_challenge] = make_pkce();
        std::string state = random_urlsafe_token(32);
        std::string nonce = random_urlsafe_token(32);

        // --- 1) /authorize ---
        const std::string authorize_url = join_url(base_url, authorize_path);

        // Build query params
        std::string query = build_query(curl, {
            {"response_type", "code"},
            {"client_id", client_id},
            {"redirect_uri", redirect_uri},
            {"scope", scope},
            {"code_challenge", code_challenge},
            {"code_challenge_method", "S256"},
            {"state", state},
            {"nonce", nonce},
        });

        HttpResp r = curl_request(
            curl,
            "GET",
            authorize_url + "?" + query,
            {},            // headers
            "",            // body
            true           // follow redirects here (land on prelogin / login UI)
        );

        // --- 2) Ensure we are on username/password login page ---
        // Even if we landed on /iam/prelogin, we hard-jump to login_path.
        if (r.effective_url.find(login_path) == std::string::npos) {
            const std::string login_url = join_url(base_url, login_path);
            r = curl_request(curl, "GET", login_url, {}, "", true);
        }

        // --- 2.5) extract CSRF from HTML ---
        const std::string csrf = extract_csrf(r.body);

        // --- 3) POST username/pass + CSRF to login ---
        const std::string login_url = join_url(base_url, login_path);

        // x-www-form-urlencoded
        std::string post_form = build_query(curl, {
            {"_csrf", csrf},
            {"username", username},
            {"password", password},
        });

        HttpResp login_resp = curl_request(
            curl,
            "POST",
            login_url,
            {"Content-Type: application/x-www-form-urlencoded"},
            post_form,
            false // DO NOT follow redirects automatically; we want Location
        );

        if (!(login_resp.status == 302 || login_resp.status == 303)) {
            curl_easy_cleanup(curl);
            throw std::runtime_error("Login did not redirect as expected. HTTP " +
                                    std::to_string(login_resp.status) +
                                    " body prefix: " + login_resp.body.substr(0, 300));
        }

        // --- 4) Follow redirects until we see code= in Location or effective URL ---
        std::string next_url = ensure_absolute_location(base_url, login_resp.location);
        if (next_url.empty()) {
            curl_easy_cleanup(curl);
            throw std::runtime_error("Login redirect missing Location header");
        }

        std::string final_url;

        for (int guard = 0; guard < 50; ++guard) {
            HttpResp step = curl_request(curl, "GET", next_url, {}, "", false);

            // if effective URL already has code=
            if (step.effective_url.find("code=") != std::string::npos) {
                final_url = step.effective_url;
                break;
            }

            // redirect?
            if (!step.location.empty()) {
                if (step.location.find("code=") != std::string::npos) {
                    final_url = ensure_absolute_location(base_url, step.location);
                    break;
                }
                next_url = ensure_absolute_location(base_url, step.location);
                continue;
            }

            // no Location and no code => dead end
            curl_easy_cleanup(curl);
            throw std::runtime_error("Stopped without redirect before code. URL=" +
                                    step.effective_url + " HTTP " + std::to_string(step.status));
        }

        if (final_url.empty()) {
            curl_easy_cleanup(curl);
            throw std::runtime_error("Failed to reach redirect_uri with code=");
        }

        // Parse code + state from final_url
        auto q = parse_query(final_url);
        auto it_code = q.find("code");
        if (it_code == q.end() || it_code->second.empty()) {
            curl_easy_cleanup(curl);
            throw std::runtime_error("No code param in final URL: " + final_url);
        }
        std::string code = it_code->second;

        auto it_state = q.find("state");
        if (it_state == q.end() || it_state->second != state) {
            curl_easy_cleanup(curl);
            throw std::runtime_error("OIDC state mismatch (possible injected response).");
        }

        // --- 5) /token exchange ---
        const std::string token_url = join_url(base_url, token_path);

        std::string token_form = build_query(curl, {
            {"grant_type", "authorization_code"},
            {"code", code},
            {"client_id", client_id},
            {"redirect_uri", redirect_uri},
            {"code_verifier", code_verifier},
        });

        HttpResp token_resp = curl_request(
            curl,
            "POST",
            token_url,
            {"Content-Type: application/x-www-form-urlencoded", "Accept: application/json"},
            token_form,
            false
        );

        if (token_resp.status < 200 || token_resp.status >= 300) {
            curl_easy_cleanup(curl);
            throw std::runtime_error("Token endpoint failed. HTTP " +
                                    std::to_string(token_resp.status) +
                                    " body: " + token_resp.body.substr(0, 400));
        }

        json j = json::parse(token_resp.body);
        OIDCLoginResult out;
        out.access_token = j.value("access_token", "");
        if (out.access_token.empty()) {
            curl_easy_cleanup(curl);
            throw std::runtime_error("Token response missing access_token");
        }

        curl_easy_cleanup(curl);
        return out;
    }

}