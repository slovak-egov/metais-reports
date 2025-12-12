#include "../include/fetch_raw.h"
#include "../include/report_client.h"
#include "../include/groovy_templates.h"
#include "../include/json_utils.h"
#include "../include/step_marker.h"
#include "../include/adaptive_pager.h"
#include "../include/pager_policy.h"
#include "../include/auth.h"
#include "../include/parallel_state.h"

#include <iostream>
#include <curl/curl.h>
#include <sys/wait.h>
#include <unistd.h>
#include <iomanip>
#include <fstream>

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

    static json fetch_one_page_array(
        const std::string& tag,
        const std::string& groovy_code,
        const std::string& bearer_token,
        const std::string& report_api_url,
        const HTTPConfig& http_cfg
    ) {
        ReportRunOptions opt;
        opt.api_url      = report_api_url;
        opt.bearer_token = bearer_token;

        HttpResponse r = run_report_groovy(opt, http_cfg, groovy_code);

        if (r.status == 401 || r.status == 403) {
            throw std::runtime_error("AUTH"); // caller decides prompt+retry
        }

        if (r.status == 0) {
            // curl failure; caller decides shrink/retry
            throw std::runtime_error("CURL:" + r.body);
        }

        if (r.status < 200 || r.status >= 300) {
            throw std::runtime_error("HTTP " + std::to_string(r.status) + ": " + r.body);
        }

        return parse_results_or_throw(r.body, tag); // uses extract_result_array ✅
    }

    static void write_page_ndjson(
        const std::filesystem::path& out_dir,
        const std::string& base,
        long offset,
        const json& arr
    ) {
        const long n   = (long)arr.size();
        const long end = (n > 0) ? (offset + n - 1) : offset;

        std::ostringstream name;
        name << base << "."
            << std::setw(9) << std::setfill('0') << offset << "."
            << std::setw(9) << std::setfill('0') << end
            << ".ndjson";

        const auto tmp = out_dir / (name.str() + ".tmp");
        const auto fin = out_dir / name.str();

        {
            std::ofstream f(tmp, std::ios::binary);
            if (!f) throw std::runtime_error("Cannot open: " + tmp.string());
            for (const auto& obj : arr) {
                if (obj.is_object()) f << obj.dump() << "\n";
            }
        }
        std::filesystem::rename(tmp, fin); // atomic finalize
    }

    static int worker_loop_parallel_fixed(
        const std::string& tag,
        const std::filesystem::path& pages_dir,
        const std::filesystem::path& state_dir,
        const std::string& report_api_url,
        const HTTPConfig& http_cfg,
        const std::string& tpl
    ) {
        const int base_limit = http_cfg.paging.page_size;

        while (true) {
            long stop_at = -1;
            if (get_stop_at(state_dir, stop_at)) {
                // if stop_at defined, and next job would be beyond, quit
                // (we check after claim too; either way ok)
            }

            long offset = claim_next_offset(state_dir, base_limit);

            if (get_stop_at(state_dir, stop_at) && offset >= stop_at) {
                return 0;
            }

            int limit = base_limit;

            while (true) {
                std::string token = read_shared_token(state_dir);
                if (token.empty()) {
                    // token not available => parent probably refreshing; wait briefly
                    usleep(200 * 1000);
                    continue;
                }

                std::string groovy_code = groovy::inject_limit_offset(tpl, limit, offset);

                try {
                    json arr = fetch_one_page_array(tag, groovy_code, token, report_api_url, http_cfg);
                    const long n = (long)arr.size();

                    if (n == 0) {
                        // first empty page => record stop boundary
                        set_stop_at(state_dir, offset);
                        return 0;
                    }

                    // Write shard
                    write_page_ndjson(pages_dir, (tag == "NODES" ? "nodes" : "rels"), offset, arr);
                    std::cout << "[" << tag << "/W] offset=" << offset << " limit=" << limit
                            << " got=" << n << "\n";
                    break; // success: go claim another offset
                } catch (const std::runtime_error& e) {
                    std::string msg = e.what();

                    if (msg == "AUTH") {
                        return 42; // signal parent to refresh token
                    }

                    // timeout-like: shrink immediately and retry same offset
                    if (msg.rfind("CURL:", 0) == 0) {
                        // Could inspect curl_code instead, but msg is ok for now.
                        limit = std::max(100, limit / 2);
                        std::cerr << "[" << tag << "/W] curl fail at offset=" << offset
                                << " -> limit=" << limit << " retry\n";
                        continue;
                    }

                    // HTTP retryable
                    if (msg.rfind("HTTP 408", 0) == 0 || msg.rfind("HTTP 504", 0) == 0 ||
                        msg.rfind("HTTP 502", 0) == 0 || msg.rfind("HTTP 503", 0) == 0) {
                        limit = std::max(100, limit / 2);
                        std::cerr << "[" << tag << "/W] retryable HTTP at offset=" << offset
                                << " -> limit=" << limit << " retry\n";
                        continue;
                    }

                    throw; // other errors: bubble up and crash worker
                }
            }
        }
    }

    static void run_parallel_fixed(
        const std::string& tag,
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        const std::string& tpl
    ) {
        std::filesystem::path base_dir = (tag == "NODES") ? layout.raw_nodes_dir : layout.raw_rels_dir;
        std::filesystem::path pages_dir = base_dir / "pages";
        std::filesystem::path state_dir = layout.tmp_dir / "paging" / tag;

        std::filesystem::create_directories(pages_dir);
        std::filesystem::create_directories(state_dir);

        // Parent resolves/prompt token once and stores it.
        std::string token = resolve_bearer_token_noninteractive(http_cfg);
        if (http_cfg.auth.mode != "none" && token.empty()) {
            token = prompt_bearer_token();
            if (token.empty()) throw std::runtime_error("No token provided.");
        }
        write_shared_token(state_dir, token);

        const std::string report_api_url = uri_cfg.report_run_url();
        const int W = std::max(1, http_cfg.paging.parallel_workers);

        auto spawn_workers = [&](std::vector<pid_t>& pids) {
            pids.clear();
            pids.reserve(W);
            for (int i = 0; i < W; ++i) {
                pid_t pid = fork();
                if (pid < 0) throw std::runtime_error("fork() failed");
                if (pid == 0) {
                    // child
                    try {
                        int rc = worker_loop_parallel_fixed(tag, pages_dir, state_dir, report_api_url, http_cfg, tpl);
                        _exit(rc);
                    } catch (const std::exception& e) {
                        std::cerr << "[" << tag << "/W] fatal: " << e.what() << "\n";
                        _exit(2);
                    }
                }
                pids.push_back(pid);
            }
        };

        std::vector<pid_t> pids;
        spawn_workers(pids);

        while (true) {
            bool any_auth = false;
            bool any_fatal = false;
            int exited = 0;

            for (pid_t pid : pids) {
                int st = 0;
                pid_t w = waitpid(pid, &st, 0);
                if (w < 0) throw std::runtime_error("waitpid failed");

                ++exited;

                if (WIFEXITED(st)) {
                    int code = WEXITSTATUS(st);
                    if (code == 42) any_auth = true;
                    else if (code != 0) any_fatal = true;
                } else {
                    any_fatal = true;
                }
            }

            if (any_fatal) {
                throw std::runtime_error("One or more workers died unexpectedly.");
            }

            if (any_auth) {
                // refresh token, respawn workers continuing from same next_offset file
                std::cerr << "[auth] Token expired. Paste a new token:\n";
                std::string newtok = prompt_bearer_token();
                if (newtok.empty()) throw std::runtime_error("No token provided.");
                write_shared_token(state_dir, newtok);

                spawn_workers(pids);
                continue;
            }

            // All workers exited cleanly (most likely stop_at reached)
            break;
        }

        // We *do not* mark_done in parallel fixed mode; pages/ is your truth.
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

        if (http_cfg.paging.mode == "parallel_fixed") {
            // parallel writes shards into <raw_dir>/pages/
            run_parallel_fixed(tag, layout, uri_cfg, http_cfg, /*tpl*/ groovy_template_string);
        }
        else run_paged("NODES", layout, uri_cfg, http_cfg, sink,
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

        if (http_cfg.paging.mode == "parallel_fixed") {
            // parallel writes shards into <raw_dir>/pages/
            run_parallel_fixed(tag, layout, uri_cfg, http_cfg, /*tpl*/ groovy_template_string);
        }
        else run_paged("RELS", layout, uri_cfg, http_cfg, sink,
            [&](int limit, long offset) {
                return groovy::inject_limit_offset(tpl, limit, offset);
            }
        );
    }

}