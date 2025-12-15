#include "../include/date.h"
#include "../include/paths_config.h"
#include "../include/http_config.h"
#include "../include/directory_layout.h"
#include "../include/project_root.h"
#include "../include/step_marker.h"
#include "../include/binary_sink.h"
#include "../include/sharded_ndjson_sink.h"
#include "../include/null_sink.h"

#include <iostream>
#include <filesystem>
#include <memory>
#include <functional>

#include "../include/fetch_enums.h"
#include "../include/fetch_metadata.h"
#include "../include/fetch_raw.h"

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

        if (metais::is_done(dir_layout.date_root)) {
            std::cout << "[info] Directory with date " << dump_date << " already exists and marked finished. Erase the .done from " << dir_layout.date_root << " and restart the script to overwrite existing data." << std::endl;
            return 0;
        }

        // 5) Create directories on disk
        dir_layout.create_all();

        std::unique_ptr<metais::BinarySink> nodes_sink;
        std::unique_ptr<metais::BinarySink> rels_sink;

        if (http_cfg.paging.mode == "parallel_fixed") {
            // parallel mode writes shards itself; sink is unused but must exist
            nodes_sink = std::make_unique<metais::NullSink>();
            rels_sink  = std::make_unique<metais::NullSink>();
        } else {
            // serial adaptive -> sharded pages (still adaptive paging!)
            nodes_sink = std::make_unique<metais::ShardedNdjsonSink>(
                dir_layout.raw_nodes_dir / "pages",
                "nodes"
            );
            rels_sink = std::make_unique<metais::ShardedNdjsonSink>(
                dir_layout.raw_rels_dir / "pages",
                "rels"
            );
        }

        // 6 fetch enums and metadata
        fetch_enums(dir_layout, uri_cfg, http_cfg);
        fetch_metadata(dir_layout, uri_cfg, http_cfg);

        // 7 fetch all data
        fetch_raw_nodes(dir_layout, uri_cfg, http_cfg, *nodes_sink);
        fetch_raw_rels (dir_layout, uri_cfg, http_cfg, *rels_sink);

    } catch (const std::exception& e) {
        std::cerr << "[ERROR] " << e.what() << "\n";
        return 1;
    }

    return 0;
}