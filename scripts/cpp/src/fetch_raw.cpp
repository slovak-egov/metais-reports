#include "../include/fetch_raw.h"
#include "../include/report_client.h"
#include "../include/groovy_templates.h"
#include "../include/json_utils.h"
#include "../include/step_marker.h"
#include "../include/adaptive_pager.h"
#include "../include/pager_policy.h"
#include "../include/auth.h"

#include <iostream>
#include <curl/curl.h>

using json = nlohmann::json;

namespace metais {

    static bool is_retryable_timeout_like(const HttpResponse& r) {
        if (r.status == 408 || r.status == 504 || r.status == 502 || r.status == 503) return true;
        if (r.status == 0 && r.curl_code == (int)CURLE_OPERATION_TIMEDOUT) return true;
        return false;
    }

    static json parse_results_or_throw(const std::string& body, const std::string& tag) {
        json j;
        try {
            j = json::parse(body);
        } catch (const std::exception& e) {
            throw std::runtime_error("[" + tag + "] Response was not valid JSON: " + std::string(e.what()) +
                                    "\nBody:\n" + body);
        }

        if (j.is_object()) {
            if (j.contains("type") && j.contains("message")) {
                throw std::runtime_error("[" + tag + "] API error object:\n" + j.dump(2));
            }
        }

        // MetaIS normalization: result/results/array
        json arr = extract_result_array(j);
        if (arr.is_array()) return arr;

        // Should never happen because extract_result_array returns array,
        // but keep a hard guard anyway:
        throw std::runtime_error("[" + tag + "] Could not extract result array from JSON.\nJSON:\n" + j.dump(2));
    }

    template <typename MakeGroovy>
    static void run_paged(
        const std::string& tag,
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        BinarySink& sink,
        MakeGroovy make_groovy
    ) {
        std::filesystem::path done_dir;
        if (tag == "NODES") done_dir = layout.raw_nodes_dir;
        else if (tag == "RELS") done_dir = layout.raw_rels_dir;
        std::string pager_policy_path = "config/json/paging_policy.json";

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

            HttpResponse r;
            try {
                r = run_report_groovy(opt, http_cfg, groovy_code);
            } catch (const std::exception& e) {
                std::string msg = e.what();
                if (msg.find("Timeout was reached") != std::string::npos ||
                    msg.find("timed out") != std::string::npos) {
                    std::cerr << "[" << tag << "] curl timeout at offset=" << offset
                            << " limit=" << limit << " -> shrinking and retrying\n";
                    pager.on_timeout_like();   // halves (or whatever your timeout_factor is)
                    continue;                  // retry SAME offset
                }
                throw; // non-timeout exception: bubble up
            }

            // Auth failure
            if (r.status == 401 || r.status == 403) {
                bearer_token = prompt_bearer_token();
                if (bearer_token.empty()) throw std::runtime_error("No token provided.");
                continue; // retry same offset
            }

            if (r.status < 200 || r.status >= 300) {
                if (is_retryable_timeout_like(r)) {
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
            if ((int)n < limit) break;

            offset += (long)n;
        }

        mark_done(done_dir);
    }

    void fetch_raw_nodes(
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        BinarySink& sink
    ) {

        const auto tpl_path = layout.project_root / "scripts/cpp/config/groovy/nodes.groovy";
        std::cout << "[NODES] template path: " << tpl_path << "\n";
        const std::string tpl = groovy::load_template_file(tpl_path);
        if (tpl.empty()) {
            throw std::runtime_error("Groovy template is empty: " + tpl_path.string());
        }

        run_paged("NODES", layout, uri_cfg, http_cfg, sink,
            [&](int limit, long offset) {
                return groovy::inject_limit_offset(tpl, limit, offset);
            }
        );
    }

    void fetch_raw_rels(
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        BinarySink& sink
    ) {

        const auto tpl_path = layout.project_root / "scripts/cpp/config/groovy/rels_all.groovy";
        std::cout << "[RELS] template path: " << tpl_path << "\n";
        const std::string tpl = groovy::load_template_file(tpl_path);
        if (tpl.empty()) {
            throw std::runtime_error("Groovy template is empty: " + tpl_path.string());
        }

        run_paged("RELS", layout, uri_cfg, http_cfg, sink,
            [&](int limit, long offset) {
                return groovy::inject_limit_offset(tpl, limit, offset);
            }
        );
    }

}