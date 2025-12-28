#pragma once
#include <string>
#include <filesystem>
#include <vector>
#include <iostream>
#include <system_error>

#include "paths_config.h"

namespace fs = std::filesystem;

namespace metais {

    static void mkdir_all(const std::vector<fs::path>& dirs, bool verbose=true) {
        for (const auto& d : dirs) {
            std::error_code ec;
            fs::create_directories(d, ec);
            if (ec) {
                throw std::runtime_error("Failed to create directory '" + d.string() + "': " + ec.message());
            }
            if (verbose) std::cout << "[mkdir] " << d << "\n";
        }
    }

    struct DirectoryLayout {
        PathsConfig cfg;
        fs::path project_root;
        std::string dump_date;

        fs::path raw_root;
        fs::path date_root;

        // existing
        fs::path metadata_root;
        fs::path enums_root;
        fs::path nodes_meta_dir;
        fs::path rels_meta_dir;

        fs::path packed_root;
        fs::path dict_dir;
        fs::path nodes_packed;
        fs::path uuids_dir;
        fs::path rels_packed;

        // raw json dumps
        fs::path raw_nodes_dir;
        fs::path raw_rels_dir;
        
        fs::path raw_nodes_pages_dir;
        fs::path raw_rels_pages_dir;
        fs::path raw_nodes_errors_dir;
        fs::path raw_rels_errors_dir;

        fs::path tmp_dir;

        // file paths
        fs::path rels_index_json;
        fs::path citypes_list_json;
        fs::path reltypes_list_json;

        DirectoryLayout(const PathsConfig& cfg_,
                        const std::string& dump_date_,
                        const fs::path& project_root_)
            : cfg(cfg_),
            project_root(project_root_),
            dump_date(dump_date_)
        {
            raw_root  = project_root / cfg.output_root;
            date_root = raw_root / dump_date;

            metadata_root  = date_root / cfg.metadata_root;
            enums_root     = date_root / cfg.enums_root;

            nodes_meta_dir = metadata_root / cfg.nodes_root;
            rels_meta_dir  = metadata_root / cfg.rels_root;

            raw_nodes_dir  = date_root / cfg.nodes_root;
            raw_rels_dir   = date_root / cfg.rels_root;

            packed_root    = date_root / cfg.packed_root;
            dict_dir       = packed_root / "dict";
            nodes_packed   = packed_root / "nodes";
            uuids_dir      = packed_root / "uuids";
            rels_packed    = packed_root / "relations";

            rels_index_json = rels_packed / "rels.json";

            raw_nodes_pages_dir = date_root / cfg.nodes_root / "pages";
            raw_rels_pages_dir  = date_root / cfg.rels_root  / "pages";
            raw_nodes_errors_dir = raw_nodes_pages_dir / "errors";
            raw_rels_errors_dir = raw_rels_pages_dir / "errors";

            tmp_dir = date_root / "tmp";

            citypes_list_json  = metadata_root / "citypes_list.json";
            reltypes_list_json = metadata_root / "reltypes_list.json";
        }

        void create_fetch_dirs(bool verbose=true) const {
            std::vector<fs::path> dirs = {
                metadata_root, enums_root, nodes_meta_dir, rels_meta_dir,
                raw_nodes_dir, raw_rels_dir,
                raw_nodes_pages_dir, raw_rels_pages_dir,
                raw_nodes_errors_dir, raw_rels_errors_dir,
                tmp_dir
            };
            mkdir_all(dirs, verbose);
        }

        void create_convert_dirs(bool verbose=true) const {
            std::vector<fs::path> dirs = {
                packed_root, dict_dir, nodes_packed, uuids_dir, rels_packed,
                tmp_dir
            };
            mkdir_all(dirs, verbose);
        }

    };

}