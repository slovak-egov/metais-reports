#include "fetch_open.h"

#include "URI_config.h"

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace metais {

    static void ensure_dir(const fs::path& dir, bool strict, bool warn_if_created, const std::string& tag) {
        if (fs::exists(dir)) return;

        std::error_code ec;
        fs::create_directories(dir, ec);

        if (ec) {
            if (strict) {
                throw std::runtime_error(
                    "Failed to create dir '" + dir.string() + "': " + ec.message()
                );
            }
            // non-strict: just continue; caller may fail later on open
            return;
        }

        if (warn_if_created) {
            std::cout << "[" << tag << "] WARNING: output dir did not exist; created: "
                      << dir << "\n";
        }
    }

    std::vector<std::string> fetch_element_list(
        const OpenFetchingSpec& spec,
        const HTTPConfig& http_cfg,
        const std::function<std::optional<std::string>(const json&)>& extract_id
    ) {
        json raw = extract_result_array(http::GET_json(spec.list_url, http_cfg));

        if (spec.log_received) {
            std::cout << "[" << spec.tag << "] " << spec.label << ": received " << raw.size()
                      << " raw entries from " << spec.list_url << "\n";
        }

        ensure_dir(spec.out_dir, spec.strict_mkdir, spec.warn_if_created, spec.tag);

        const fs::path list_path = spec.out_dir / spec.out_filename;
        {
            std::ofstream out(list_path);
            if (!out.is_open()) {
                throw std::runtime_error("Failed to write " + spec.out_filename + " at " + list_path.string());
            }
            out << raw.dump(2);
        }

        if (spec.log_written) {
            std::cout << "[" << spec.tag << "] Saved -> " << list_path << "\n";
        }

        std::vector<std::string> ids;
        ids.reserve(raw.size());

        for (const auto& item : raw) {
            if (!item.is_object()) continue;
            if (auto id = extract_id(item)) {
                if (!id->empty()) ids.push_back(*id);
            }
        }
        return ids;
    }

    json fetch_detail(
        const std::string& detail_api_code,
        const HTTPConfig& http_cfg,
        const OpenFetchingSpec& spec
    ) {
        const std::string url = replace_all(spec.detail_url_tpl, "{name}", detail_api_code);
        json detail = http::GET_json(url, http_cfg);

        if (spec.log_received) {
            std::cout << "[" << spec.tag << "] received " << detail_api_code
                      << " from " << url << "\n";
        }

        ensure_dir(spec.out_dir, spec.strict_mkdir, spec.warn_if_created, spec.tag);

        //std::string fname = spec.out_filename.empty() ? (detail_api_code + ".json") : spec.out_filename;
        std::string fname = detail_api_code + ".json";
        const fs::path out_path = spec.out_dir / fname;
        json payload = spec.transform(detail);

        std::ofstream out(out_path);
        if (!out.is_open()) {
            throw std::runtime_error("Failed to write " + out_path.string());
        }
        out << payload.dump(2);

        if (spec.log_written) {
            std::cout << "[" << spec.tag << "] " << spec.kind << " " << detail_api_code
                      << " -> " << out_path << "\n";
        }

        return payload;
    }

}