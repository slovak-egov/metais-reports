#include "../include/fetch_raw.h"
#include "../include/report_client.h"
#include "../include/groovy_templates.h"
#include "../include/json_utils.h"
#include "../include/step_marker.h"
#include "../include/adaptive_pager.h"
#include "../include/pager_policy.h"
#include "../include/auth.h"
#include "../include/parallel_state.h"
#include "../include/sharded_ndjson_sink.h"

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

    static HttpResponse fetch_one_page_response(
        const std::string& groovy_code,
        const std::string& bearer_token,
        const std::string& report_api_url,
        const HTTPConfig& http_cfg
    ) {
        ReportRunOptions opt;
        opt.api_url      = report_api_url;
        opt.bearer_token = bearer_token;
        return run_report_groovy(opt, http_cfg, groovy_code);
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
        const int limit = base_limit; // fixed window in parallel mode
        const std::string base_name = (tag == "NODES") ? "nodes" : "rels";

        while (true) {
            long stop_at = -1;
            if (get_stop_at(state_dir, stop_at) && stop_at >= 0) {
                // cheap early exit check (real check after claim too)
            }

            long offset = claim_next_offset(state_dir, base_limit);

            if (get_stop_at(state_dir, stop_at) && stop_at >= 0 && offset >= stop_at) {
                return 0;
            }

            // if already written (restart/rewind), skip
            const auto fin = shard_path(pages_dir, base_name, offset);
            if (std::filesystem::exists(fin)) {
                continue;
            }

            for (;;) {
                std::string token = read_shared_token(state_dir);
                if (token.empty()) {
                    usleep(200 * 1000);
                    continue;
                }

                std::string groovy_code = groovy::inject_limit_offset(tpl, limit, offset);
                HttpResponse r = fetch_one_page_response(groovy_code, token, report_api_url, http_cfg);

                // token expired -> parent refresh + rewind
                if (r.status == 401 || r.status == 403) {
                    record_failed_offset(state_dir, offset);
                    return 42;
                }

                // curl-level failure after report_client retries
                if (r.status == 0) {
                    if (get_stop_at(state_dir, stop_at) && stop_at >= 0 && offset >= stop_at) return 0;

                    std::cerr << "[" << tag << "/W] curl error at offset=" << offset
                            << " (curl_code=" << r.curl_code << "): " << r.body
                            << " -> retry\n";
                    usleep(500 * 1000);
                    continue;
                }

                // retryable HTTP after report_client retries
                if (r.status == 408 || r.status == 429 || r.status == 500 ||
                    r.status == 502 || r.status == 503 || r.status == 504) {
                    std::cerr << "[" << tag << "/W] HTTP " << r.status
                            << " at offset=" << offset << " -> retry\n";
                    usleep(500 * 1000);
                    continue;
                }

                if (r.status < 200 || r.status >= 300) {
                    throw std::runtime_error("[" + tag + "/W] HTTP " + std::to_string(r.status) +
                                            " at offset=" + std::to_string(offset) +
                                            " body:\n" + r.body);
                }

                json arr = parse_results_or_throw(r.body, tag);
                const long n = (long)arr.size();

                if (n == 0) {
                    set_stop_at(state_dir, offset);
                    return 0;
                }

                write_shard_ndjson(pages_dir, base_name, offset, arr);

                std::cout << "[" << tag << "/W] offset=" << offset
                        << " limit=" << limit << " got=" << n << "\n";
                break; // success -> next offset
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
        namespace fs = std::filesystem;

        fs::path base_dir  = (tag == "NODES") ? layout.raw_nodes_dir : layout.raw_rels_dir;
        fs::path pages_dir = base_dir / "pages";
        fs::path state_dir = layout.tmp_dir / "paging" / tag;

        fs::create_directories(pages_dir);
        fs::create_directories(state_dir);

        auto refresh_token = [&]() {
            std::string tok = resolve_bearer_token_noninteractive(http_cfg);
            if (http_cfg.auth.mode != "none" && tok.empty()) {
                tok = prompt_bearer_token();
                if (tok.empty()) throw std::runtime_error("No token provided.");
            }
            write_shared_token(state_dir, tok);
        };

        refresh_token();

        const std::string report_api_url = uri_cfg.report_run_url();
        const int W = std::max(1, http_cfg.paging.parallel_workers);

        auto spawn_workers = [&](std::vector<pid_t>& pids) {
            pids.clear();
            for (int i = 0; i < W; ++i) {
                pid_t pid = fork();
                if (pid < 0) throw std::runtime_error("fork() failed");
                if (pid == 0) {
                    try {
                        int rc = worker_loop_parallel_fixed(
                            tag, pages_dir, state_dir,
                            report_api_url, http_cfg, tpl
                        );
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
            bool need_auth = false;
            bool fatal = false;

            while (!pids.empty()) {
                for (auto it = pids.begin(); it != pids.end(); ) {
                    int st = 0;
                    pid_t w = waitpid(*it, &st, WNOHANG);
                    if (w == 0) { ++it; continue; }
                    if (w < 0) throw std::runtime_error("waitpid failed");

                    if (WIFEXITED(st)) {
                        int code = WEXITSTATUS(st);
                        if (code == 42) need_auth = true;
                        else if (code != 0) fatal = true;
                    } else {
                        fatal = true;
                    }

                    it = pids.erase(it);
                }

                if (need_auth || fatal) break;
                usleep(100 * 1000);
            }

            if (fatal) {
                for (pid_t pid : pids) kill(pid, SIGKILL);
                throw std::runtime_error("One or more workers died unexpectedly.");
            }

            if (need_auth) {
                // stop remaining workers
                for (pid_t pid : pids) kill(pid, SIGKILL);

                // reap them (avoid zombies)
                for (pid_t pid : pids) {
                    int st = 0;
                    waitpid(pid, &st, 0);
                }
                pids.clear();

                std::cerr << "[auth] Token expired. Paste a new token:\n";
                refresh_token();

                long mn_fail = -1;
                if (read_and_clear_min_failed_offset(state_dir, mn_fail)) {
                    write_next_offset_if_smaller(state_dir, mn_fail);
                    std::cerr << "[auth] rewinding next_offset to " << mn_fail << "\n";
                }

                spawn_workers(pids);
                continue;
            }

            // all workers exited cleanly
            break;
        }

        // no mark_done: pages/ is authoritative
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
                usleep(300 * 1000);
                continue;
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
            //if ((int)n < limit) break;

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

        const auto tpl_path = layout.project_root / "scripts/cpp/config/groovy/node.groovy";
        std::cout << "[NODES] template path: " << tpl_path << "\n";
        const std::string tpl = groovy::load_template_file(tpl_path);
        if (tpl.empty()) {
            throw std::runtime_error("Groovy template is empty: " + tpl_path.string());
        }

        if (http_cfg.paging.mode == "parallel_fixed") {
            run_parallel_fixed("NODES", layout, uri_cfg, http_cfg, tpl);
            return;
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

        const auto tpl_path = layout.project_root / "scripts/cpp/config/groovy/relation.groovy";
        std::cout << "[RELS] template path: " << tpl_path << "\n";
        const std::string tpl = groovy::load_template_file(tpl_path);
        if (tpl.empty()) {
            throw std::runtime_error("Groovy template is empty: " + tpl_path.string());
        }

        if (http_cfg.paging.mode == "parallel_fixed") {
            run_parallel_fixed("RELS", layout, uri_cfg, http_cfg, tpl);
            return;
        }
        else run_paged("RELS", layout, uri_cfg, http_cfg, sink,
            [&](int limit, long offset) {
                return groovy::inject_limit_offset(tpl, limit, offset);
            }
        );
    }

}
