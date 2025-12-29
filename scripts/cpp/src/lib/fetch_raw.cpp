#include "fetch_raw.h"
#include "groovy_templates.h"
#include "step_marker.h"
#include "adaptive_pager.h"
#include "pager_policy.h"
#include "auth.h"
#include "json_utils.h"
#include "metais_response.h"
#include "fetch_post.h"
#include "http_response.h"
#include "shard_naming.h"
#include "traverse_raw.h"
#include "shard_naming.h"

#include <iostream>
#include <curl/curl.h>
#include <fstream>
#include <thread>
#include <chrono>
#include <string>
#include <optional>
#include <filesystem>
#include <sstream>
#include <iomanip>

using json = nlohmann::json;
namespace fs = std::filesystem;

using metais::kShardPad;

namespace {

    static bool starts_with(const std::string& s, const std::string& prefix) {
        return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
    }

    [[maybe_unused]]
    static fs::path meta_path_for(const fs::path& pages_dir, const std::string& base, long offset) {
        std::ostringstream name;
        name << base << "." << std::setw(kShardPad) << std::setfill('0') << offset << ".meta.json";
        return pages_dir / name.str();
    }

    static void write_text_file(const std::filesystem::path& p, const std::string& s) {
        std::filesystem::create_directories(p.parent_path());
        std::ofstream f(p, std::ios::binary);
        if (!f) throw std::runtime_error("Failed to open for write: " + p.string());
        f.write(s.data(), (std::streamsize)s.size());
    }

    static bool is_timeout_like(const metais::HttpResponse& r) {
        if (r.status == 408 || r.status == 504 || r.status == 502 || r.status == 503) return true;
        if (r.status == 0 && r.curl_code == (int)CURLE_OPERATION_TIMEDOUT) return true;
        return false;
    }

    static std::filesystem::path error_path_for(
        const std::filesystem::path& errors_dir,
        const std::string& base,
        long bad_offset
    ) {
        std::ostringstream name;
        name << base << "." << std::setw(kShardPad) << std::setfill('0') << bad_offset << ".error.json";
        return errors_dir / name.str();
    }

    static bool is_hard_page_error(const metais::HttpResponse& r) {
        if (is_timeout_like(r)) return false;
        if (r.status == 401 || r.status == 403) return false;
        // Everything else 4xx/5xx is treated as hard here.
        return (r.status < 200 || r.status >= 300);
    }

}

namespace metais {

    HttpResponse run_report_groovy(
        const ReportRunOptions& opt,
        const HTTPConfig& http_cfg,
        const std::string& groovy_code,
        const json& params
    ) {
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

    ResumePoint find_resume_point(const fs::path& pages_dir, const std::string& base) {
        ResumePoint best;

        if (!fs::exists(pages_dir)) return best;

        for (const auto& entry : fs::directory_iterator(pages_dir)) {
            if (!entry.is_regular_file()) continue;

            const std::string fname = entry.path().filename().string();
            auto off_opt = parse_offset_from_meta_filename(fname, base);
            if (!off_opt) continue;

            const long offset = *off_opt;
            const fs::path meta_path = entry.path();
            const fs::path data_path = metais::shard_data_path(pages_dir, base, offset);

            // If meta exists but data missing -> delete meta, ignore
            if (!fs::exists(data_path)) {
                std::error_code ec;
                fs::remove(meta_path, ec);
                continue;
            }

            // Try parse meta JSON
            nlohmann::json meta;
            try {
                meta = load_json_file(meta_path.string());
            } catch (...) {
                // broken meta -> delete both meta + data, ignore
                std::error_code ec;
                fs::remove(meta_path, ec);
                fs::remove(data_path, ec);
                continue;
            }

            // Validate required fields
            if (!meta.contains("offset") || !meta.contains("received")) continue;
            if (!meta["offset"].is_number_integer()) continue;
            if (!meta["received"].is_number_integer()) continue;

            const long meta_offset = meta["offset"].get<long>();
            const long received    = meta["received"].get<long>();
            if (meta_offset != offset) {
                // mismatch -> delete both, ignore
                std::error_code ec;
                fs::remove(meta_path, ec);
                fs::remove(data_path, ec);
                continue;
            }
            if (received < 0) continue;

            int limit = 0;
            if (meta.contains("limit") && meta["limit"].is_number_integer())
                limit = meta["limit"].get<int>();

            // Keep the highest offset page
            if (!best.found || meta_offset > (best.next_offset - 1)) {
                best.found = true;
                best.last_limit = limit;
                best.next_offset = meta_offset + received; // IMPORTANT
            }
        }

        return best;
    }

    template <typename MakeGroovy>
    static HttpResponse try_run(
        const ReportRunOptions& base_opt,
        const HTTPConfig& http_cfg,
        const json& params,
        MakeGroovy make_groovy
    ) {
        const std::string code = make_groovy(base_opt.limit, base_opt.offset);
        return metais::run_report_groovy(base_opt, http_cfg, code, params);
    }

    template <typename MakeGroovyFull>
    static long bisect_bad_offset(
        const std::string&, // tag
        const HTTPConfig& http_cfg,
        const ReportRunOptions& base_opt,
        const json& params,
        MakeGroovyFull make_full
    ) {
        long off = base_opt.offset;
        int  lim = base_opt.limit;

        // Safety cap: prevents endless loops if backend behaves weirdly.
        int guard = 0;

        while (lim > 1) {
            if (++guard > 64) break;

            const int left = lim / 2;
            const int right = lim - left;

            // Test left half
            {
                ReportRunOptions opt = base_opt;
                opt.offset = off;
                opt.limit  = left;
                HttpResponse r = try_run(opt, http_cfg, params, make_full);

                if (is_hard_page_error(r)) {
                    // culprit is in left half
                    lim = left;
                    continue;
                }
            }

            // Otherwise culprit is in right half
            off = off + left;
            lim = right;
        }

        return off; // best guess (should be the single failing position)
    }

    template <typename MakeGroovyUuid>
    static std::optional<std::string> fetch_uuid_at(
        const std::string& tag,
        const HTTPConfig& http_cfg,
        const ReportRunOptions& base_opt,
        const json& params,
        MakeGroovyUuid make_uuid
    ) {
        ReportRunOptions opt = base_opt;
        opt.limit = 1;

        HttpResponse r = try_run(opt, http_cfg, params, make_uuid);
        if (r.status < 200 || r.status >= 300) return std::nullopt;

        // Your parse_results_or_throw expects normal shape.
        try {
            json arr = parse_results_or_throw(r.body, tag);
            if (!arr.is_array() || arr.empty()) return std::nullopt;

            const json& obj = arr.at(0);
            if (!obj.is_object()) return std::nullopt;

            // adjust to whatever your uuid-only template returns.
            // Typical: {"uuid":"..."} or {"uuid":"...","type":"..."}.
            if (obj.contains("uuid") && obj["uuid"].is_string())
                return obj["uuid"].get<std::string>();

            // fallback: maybe raw uses "UUID" or something
            for (auto it = obj.begin(); it != obj.end(); ++it) {
                if (it.value().is_string()) {
                    const std::string s = it.value().get<std::string>();
                    // if it looks like UUID-ish, accept it (keep this conservative)
                    if (s.size() >= 32 && s.size() <= 40) return s;
                }
            }

            return std::nullopt;
        } catch (...) {
            return std::nullopt;
        }
    }

    template <typename MakeGroovyFull, typename MakeGroovySafe>
    static void run_paged(
        const std::string& tag,
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        PageSink& sink,
        const json& params,
        MakeGroovyFull make_groovy,
        MakeGroovySafe make_groovy_safe
    ) {
        bool interactive_allowed = http_cfg.auth.required;

        fs::path done_dir;
        if (tag == "NODES") done_dir = layout.raw_nodes_dir;
        else if (tag == "RELS") done_dir = layout.raw_rels_dir;

        const auto pager_policy_path =
            layout.project_root / "scripts/cpp/config/json/paging_policy.json";

        if (is_done(done_dir)) {
            std::cout << "[" << tag << "] .done marker present in " << done_dir << " - skipping.\n";
            return;
        }

        const std::string report_api_url = uri_cfg.report_run_url();

        std::string bearer_token = resolve_bearer_token_noninteractive(http_cfg, http_cfg.auth.required, uri_cfg.base_url);
        if (http_cfg.auth.mode != "none" && bearer_token.empty()) {
            if (!http_cfg.auth.required) {
                throw std::runtime_error("Bearer token missing (auth.required=false; not prompting)");
            }
            bearer_token = prompt_bearer_token();
            if (bearer_token.empty()) {
                throw std::runtime_error("Bearer token required for report POST but empty");
            }
        }

        PagerPolicy pol = load_pager_policy(pager_policy_path);

        // ---- resume logic HERE (after we know tag/layout, before pager+offset) ----
        fs::path pages_dir, errs_dir;
        std::string base;
        if (tag == "NODES") {
            pages_dir = layout.raw_nodes_pages_dir;
            errs_dir = layout.raw_nodes_errors_dir;
            base = "nodes";
        }
        else {
            pages_dir = layout.raw_rels_pages_dir;
            errs_dir = layout.raw_rels_errors_dir;
            base = "rels";
        }

        ResumePoint rp = find_resume_point(pages_dir, base);

        int initial_limit = http_cfg.paging.page_size;
        long offset = 0;

        if (rp.found) {
            offset = rp.next_offset;
            if (rp.last_limit > 0) initial_limit = rp.last_limit;
            std::cout << "[" << tag << "] resume: next_offset=" << offset
                    << " initial_limit=" << initial_limit << "\n";
        }

        AdaptivePager pager(initial_limit, pol);
        while (true) {
            int limit = pager.limit();

            std::string groovy_code = make_groovy(limit, offset);

            ReportRunOptions opt;
            opt.api_url       = report_api_url;
            opt.bearer_token  = bearer_token;
            opt.limit         = limit;
            opt.offset        = offset;

            HttpResponse r = run_report_groovy(opt, http_cfg, groovy_code, params);

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
                auto d = handle_auth_challenge(http_cfg, r, bearer_token, interactive_allowed, uri_cfg.base_url);
                if (d == AuthDecision::Retry) {
                    continue; // retry same offset with new token
                }
                throw std::runtime_error("[" + tag + "] Auth failure: HTTP " + std::to_string(r.status));
            }

            if (r.status < 200 || r.status >= 300) {
                if (is_timeout_like(r)) {
                    std::cerr << "[" << tag << "] HTTP " << r.status
                            << " at offset=" << offset << " limit=" << limit
                            << " -> halving page size and retrying\n";
                    pager.on_timeout_like();
                    continue;
                }

                // isolate + report + skip ----
                std::cerr << "[" << tag << "] HARD HTTP " << r.status
                        << " at offset=" << offset << " limit=" << limit
                        << " -> isolating failing record via bisection\n";

                // Prepare base options
                ReportRunOptions base_opt = opt;

                // 1) bisect to find bad offset
                long bad_offset;
                if (limit == 1) { bad_offset = offset; }
                else { bad_offset = bisect_bad_offset(tag, http_cfg, base_opt, params, make_groovy); }

                // 1b) capture the isolated failing response (full template, limit=1)
                ReportRunOptions single = base_opt;
                single.offset = bad_offset;
                single.limit  = 1;

                HttpResponse r_single = try_run(single, http_cfg, params, make_groovy);
                int auth_tries = 0;
                while ((r_single.status == 401 || r_single.status == 403) && auth_tries++ < 2) {
                    auto d = handle_auth_challenge(http_cfg, r_single, bearer_token, interactive_allowed, uri_cfg.base_url);
                    if (d != AuthDecision::Retry) break;
                    base_opt.bearer_token = bearer_token;
                    single.bearer_token = bearer_token;
                    r_single = try_run(single, http_cfg, params, make_groovy);
                }

                // 2) attempt uuid-only at bad_offset
                base_opt.offset = bad_offset;
                base_opt.limit  = 1;
                base_opt.bearer_token = bearer_token;
                single.bearer_token   = bearer_token;
                auto bad_uuid = fetch_uuid_at(tag, http_cfg, base_opt, params, make_groovy_safe);

                // 3) write error report
                json report;
                report["tag"] = tag;
                report["bad_offset"] = bad_offset;
                report["page_offset"] = offset;
                report["page_limit"] = limit;

                // original page error
                report["page_http_status"] = r.status;
                report["page_seconds"] = r.seconds;
                report["page_error_body"] = r.body;

                // isolated single-record error
                report["single_http_status"] = r_single.status;
                report["single_seconds"] = r_single.seconds;
                report["single_error_body"] = r_single.body;

                report["uuid"] = bad_uuid ? json(*bad_uuid) : json(nullptr);

                const fs::path ep = error_path_for(errs_dir, base, bad_offset);
                write_text_file(ep, report.dump(2));

                std::cerr << "[" << tag << "] Logged bad record at offset=" << bad_offset
                        << (bad_uuid ? (" uuid=" + *bad_uuid) : " uuid=(unavailable)")
                        << " -> skipping 1 and continuing\n";

                // 4) skip exactly one record
                offset = bad_offset + 1;

                // Optional: shrink pager a bit because backend is cranky
                pager.on_timeout_like();

                continue;
            }

            json arr = parse_results_or_throw(r.body, tag);
            const std::size_t n = arr.size();

            sink.begin_page(offset, limit);
            for (const auto& obj : arr) {
                if (obj.is_object()) sink.write_item(obj);
            }
            PageStats st;
            st.offset = offset;
            st.limit = limit;
            st.received = n;
            st.seconds = r.seconds;
            sink.end_page(st);

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
        const fs::path& tpl_path,
        const fs::path& tpl_path_safe,
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        PageSink& sink
    ) {
        std::cout << "[" << tag << "] template path: " << tpl_path << "\n";
        std::cout << "[" << tag << "] safe template path: " << tpl_path_safe << "\n";
        const std::string tpl = groovy::load_template_file(tpl_path);
        const std::string tpl_safe = groovy::load_template_file(tpl_path_safe);
        if (tpl.empty()) throw std::runtime_error("Groovy template is empty: " + tpl_path.string());
        if (tpl_safe.empty()) throw std::runtime_error("Safe Groovy template is empty: " + tpl_path_safe.string());

        const fs::path params_path = layout.project_root / "scripts/cpp/config/params/params.json";

        if (!fs::exists(params_path)) {
            throw std::runtime_error("params.json not found: " + params_path.string());
        }

        json params = load_json_file(params_path.string());

        run_paged(tag, layout, uri_cfg, http_cfg, sink, params,
            [&](int limit, long offset) {
                return groovy::inject_limit_offset(tpl, limit, offset);
            },
            [&](int limit, long offset) {
                return groovy::inject_limit_offset(tpl_safe, limit, offset);
            }
        );
    }

    void fetch_raw_nodes(
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        PageSink& sink
    ) {
        fs::path groovy_path = layout.project_root / "scripts/cpp/config/groovy/node.groovy";
        fs::path groovy_path_safe = layout.project_root / "scripts/cpp/config/groovy/node_safe.groovy";
        fetch_raw_common("NODES", groovy_path, groovy_path_safe,
            layout, uri_cfg, http_cfg, sink);
    }

    void fetch_raw_rels(
        const DirectoryLayout& layout,
        const URIConfig& uri_cfg,
        const HTTPConfig& http_cfg,
        PageSink& sink
    ) {
        fs::path groovy_path = layout.project_root / "scripts/cpp/config/groovy/relation.groovy";
        fs::path groovy_path_safe = layout.project_root / "scripts/cpp/config/groovy/relation_safe.groovy";
        fetch_raw_common("RELS", groovy_path, groovy_path_safe,
            layout, uri_cfg, http_cfg, sink);
    }

}
