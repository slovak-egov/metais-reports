#include "date.h"
#include "paths_config.h"
#include "http_config.h"
#include "directory_layout.h"
#include "project_root.h"
#include "step_marker.h"
#include "page_sink.h"
#include "sharded_ndjson_sink.h"

#include <iostream>
#include <filesystem>
#include <memory>
#include <functional>

#include "fetch_enums.h"
#include "fetch_codelists.h"
#include "fetch_metadata.h"
#include "fetch_raw.h"

namespace fs = std::filesystem;

int main() {
    try {
        // Resolve dump date
        std::string dump_date = today_date();
        std::cout << "[info] dump date = " << dump_date << "\n";

        // Load paths config (with fallbacks)
        auto path_cfg = metais::load_paths_config("config/json/paths.json");
        auto uri_cfg  = metais::load_uri_config("config/json/URI.json");
        auto http_cfg = metais::load_http_settings("config/json/http_config.json");

        // Decide project root.
        // we're at: .../metais-reports/scripts/cpp
        // it keeps going up till it finds file called .git
        fs::path cwd = fs::current_path();
        fs::path project_root = metais::find_project_root();

        std::cout << "[info] cwd          = " << cwd << "\n";
        std::cout << "[info] project_root = " << project_root << "\n";

        // Build directory ladir_layoutout
        metais::DirectoryLayout dir_layout(path_cfg, dump_date, project_root);

        /*
        if (metais::is_done(dir_layout.date_root)) {
            std::cout << "[info] Directory with date " << dump_date << " already exists and marked finished. Erase the .done from " << dir_layout.date_root << " and restart the script to overwrite existing data." << std::endl;
            return 0;
        }*/

        // 5) Create directories on disk
        dir_layout.create_fetch_dirs();

        std::unique_ptr<metais::PageSink> nodes_sink =
            std::make_unique<metais::ShardedNdjsonSink>(dir_layout.raw_nodes_dir / "pages", "nodes");

        std::unique_ptr<metais::PageSink> rels_sink =
            std::make_unique<metais::ShardedNdjsonSink>(dir_layout.raw_rels_dir / "pages", "rels");

        // 6 fetch enums and metadata
        fetch_enums(dir_layout, uri_cfg, http_cfg);
        fetch_codelists(dir_layout, uri_cfg, http_cfg);
        fetch_metadata(dir_layout, uri_cfg, http_cfg);

        // 7 fetch all data
        fetch_raw_nodes(dir_layout, uri_cfg, http_cfg, *nodes_sink);
        fetch_raw_rels (dir_layout, uri_cfg, http_cfg, *rels_sink);

    } catch (const std::length_error& e) {
        std::cerr << "[ERROR] length_error: " << e.what() << "\n";
        return 1;
    } catch (const std::bad_alloc& e) {
        std::cerr << "[ERROR] bad_alloc: " << e.what() << "\n";
        return 1;
    } catch (const std::exception& e) {
        std::cerr << "[ERROR] exception: " << e.what() << "\n";
        return 1;
    }

    return 0;
}