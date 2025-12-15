#include "../include/fetch_raw.h"
#include "../include/groovy_templates.h"
#include "../include/step_marker.h"
#include "../include/adaptive_pager.h"
#include "../include/pager_policy.h"
#include "../include/auth.h"
#include "../include/json_utils.h"
#include "../include/metais_response.h"
#include "../include/fetch_post.h"

#include <iostream>
#include <curl/curl.h>
#include <fstream>
#include <thread>
#include <chrono>

using json = nlohmann::json;

namespace metais {

    HttpResponse run_report_groovy(
        const ReportRunOptions& opt,
        const HTTPConfig& http_cfg,
        const std::string& groovy_code
    ) {
        json params = load_json_file("config/params/params.json");
        json payload;
        payload["body"] = groovy_code;
        payload["parameters"] = params;

        PostFetchingSpec s;
        s.tag = "REPORT";
        s.label = "run";
        s.api_url = opt.api_url;
        s.payload = payload;
        s.parse_json = false; // you currently treat body as string
        s.follow_redirects = true;
        s.auth_header = "Authorization: Bearer " + opt.bearer_token;

        PostResult r = fetch_post(s, http_cfg);

        HttpResponse out;
        out.seconds = r.seconds;

        if (!r.transport_ok) {
            out.status = 0;
            out.curl_code = r.curl_code;
            out.body = r.raw_body.empty() ? std::string("curl: ") + std::to_string(r.curl_code) : r.raw_body;
            return out;
        }

        out.status = r.http_code;
        out.body = r.raw_body;
        return out;
    }

    static bool is_timeout_like(const HttpResponse& r) {
        if (r.status == 408 || r.status == 504 || r.status == 502 || r.status == 503) return true;
        if (r.status == 0 && r.curl_code == (int)CURLE_OPERATION_TIMEDOUT) return true;
        return false;
    }

    template <typename MakeGroovy>
    static void run_paged(
        const std::string& tag,
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        PageSink& sink,
        MakeGroovy make_groovy
    ) {
        std::filesystem::path done_dir;
        if (tag == "NODES") done_dir = layout.raw_nodes_dir;
        else if (tag == "RELS") done_dir = layout.raw_rels_dir;
        const auto pager_policy_path = layout.project_root / "scripts/cpp/config/json/paging_policy.json";

        if (is_done(done_dir)) {
            std::cout << "[" << tag << "] .done marker present in " << done_dir << " - skipping.\n";
            return;
        }

        const std::string report_api_url = uri_cfg.report_run_url();

        // resolve now and then repromptable on 401/403
        std::string bearer_token = resolve_bearer_token_noninteractive(http_cfg);

        // For report POST, we need a token. If it's missing, prompt now.
        if (http_cfg.auth.mode != "none" && bearer_token.empty()) {
            // If you want "required=false" to avoid prompting, gate it here:
            if (!http_cfg.auth.required) {
                throw std::runtime_error("Bearer token missing (auth.required=false; not prompting)");
            }

            bearer_token = prompt_bearer_token();
            if (bearer_token.empty()) {
                throw std::runtime_error("Bearer token required for report POST but empty");
            }
        }

        PagerPolicy pol = load_pager_policy(pager_policy_path);
        AdaptivePager pager(http_cfg.paging.page_size, pol);

        long offset = 0;
        while (true) {
            int limit = pager.limit();

            std::string groovy_code = make_groovy(limit, offset);

            ReportRunOptions opt;
            opt.api_url       = report_api_url;
            opt.bearer_token  = bearer_token;
            opt.limit         = limit;
            opt.offset        = offset;

            HttpResponse r = run_report_groovy(opt, http_cfg, groovy_code);

            if (r.status == 0 && r.curl_code == (int)CURLE_OPERATION_TIMEDOUT) {
                std::cerr << "[" << tag << "] curl timeout at offset=" << offset
                        << " limit=" << limit << " -> shrinking and retrying\n";
                pager.on_timeout_like();
                continue;
            }
            if (r.status == 0) {
                std::cerr << "[" << tag << "] curl failure at offset=" << offset
                        << " limit=" << limit << " (curl_code=" << r.curl_code
                        << "): " << r.body << " -> retry\n";
                // maybe small sleep/backoff
                std::this_thread::sleep_for(std::chrono::milliseconds(300));
                continue;
            }

            // Auth failure
            if (r.status == 401 || r.status == 403) {
                bearer_token = prompt_bearer_token();
                if (bearer_token.empty()) throw std::runtime_error("No token provided.");
                continue; // retry same offset
            }

            if (r.status < 200 || r.status >= 300) {
                if (is_timeout_like(r)) {
                    std::cerr << "[" << tag << "] HTTP " << r.status
                              << " at offset=" << offset << " limit=" << limit
                              << " -> halving page size and retrying\n";
                    pager.on_timeout_like();
                    continue;
                }

                throw std::runtime_error(
                    "[" + tag + "] HTTP " + std::to_string(r.status) + " body: " + r.body
                );
            }

            json arr = parse_results_or_throw(r.body, tag);
            const std::size_t n = arr.size();

            sink.begin_page(offset, limit);
            for (const auto& obj : arr) {
                if (obj.is_object()) sink.write_item(obj);
            }
            sink.end_page(n);

            std::cout << "[" << tag << "] offset=" << offset << " limit=" << limit
                      << " got=" << n << " in " << r.seconds << "s\n";

            pager.on_success(r.seconds);

            if (n == 0) break;
            //if ((int)n < limit) break;

            offset += (long)n;
        }

        mark_done(done_dir);
    }

    static void fetch_raw_common(
        const std::string& tag,
        const std::filesystem::path& tpl_path,
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        PageSink& sink
    ) {
        std::cout << "[" << tag << "] template path: " << tpl_path << "\n";
        const std::string tpl = groovy::load_template_file(tpl_path);
        if (tpl.empty()) throw std::runtime_error("Groovy template is empty: " + tpl_path.string());

        run_paged(tag, layout, uri_cfg, http_cfg, sink,
            [&](int limit, long offset) {
                return groovy::inject_limit_offset(tpl, limit, offset);
            }
        );
    }

    void fetch_raw_nodes(
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        PageSink& sink
    ) {
        fetch_raw_common("NODES", layout.project_root / "scripts/cpp/config/groovy/node.groovy",
            layout, uri_cfg, http_cfg, sink);
    }

    void fetch_raw_rels(
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        PageSink& sink
    ) {
        fetch_raw_common("RELS", layout.project_root / "scripts/cpp/config/groovy/relation.groovy",
            layout, uri_cfg, http_cfg, sink);
    }

}
