#include "../include/date.h"
#include "../include/paths_config.h"
#include "../include/http_config.h"
#include "../include/directory_layout.h"
#include "../include/project_root.h"
#include "../include/step_marker.h"
#include "../include/binary_sink.h"

#include <iostream>
#include <filesystem>

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

        // Build directory layout
        metais::DirectoryLayout layout(path_cfg, dump_date, project_root);

        if (metais::is_done(layout.date_root)) {
            std::cout << "[info] Directory with date " << dump_date << " already exists and marked finished. Erase the .done from " << layout.date_root << " and restart the script to overwrite existing data." << std::endl;
            return 0;
        }

        // 5) Create directories on disk
        layout.create_all();

        metais::NdjsonSink nodes_sink(layout.raw_nodes_dir / "nodes.ndjson");
        metais::NdjsonSink rels_sink (layout.raw_rels_dir  / "rels.ndjson");

        // 6 fetch enums and metadata
        fetch_enums(layout, uri_cfg, http_cfg);
        fetch_metadata(layout, uri_cfg, http_cfg);

        // 7 fetch all data
        fetch_raw_nodes(layout, uri_cfg, http_cfg, nodes_sink);
        fetch_raw_rels(layout, uri_cfg, http_cfg, rels_sink);

        /*
        std::cout << "[info] date_root      = " << layout.date_root      << "\n";
        std::cout << "[info] metadata_root  = " << layout.metadata_root  << "\n";
        std::cout << "[info] enums_dir      = " << layout.enums_dir      << "\n";
        std::cout << "[info] nodes_meta_dir = " << layout.nodes_meta_dir << "\n";
        std::cout << "[info] rels_meta_dir  = " << layout.rels_meta_dir  << "\n";
        std::cout << "[info] packed_root    = " << layout.packed_root    << "\n";
        std::cout << "[info] dict_dir       = " << layout.dict_dir       << "\n";
        std::cout << "[info] nodes_packed   = " << layout.nodes_packed   << "\n";
        std::cout << "[info] uuid_index_dir = " << layout.uuid_index_dir << "\n";
        std::cout << "[info] uuid_types_dir = " << layout.uuid_types_dir << "\n";
        std::cout << "[info] rels_packed    = " << layout.rels_packed    << "\n";
        std::cout << "[info] tmp_dir        = " << layout.tmp_dir        << "\n";
        */

    } catch (const std::exception& e) {
        std::cerr << "[ERROR] " << e.what() << "\n";
        return 1;
    }

    return 0;
}