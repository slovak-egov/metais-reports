#include "shard_naming.h"
#include <iomanip>
#include <sstream>

namespace metais {

    std::filesystem::path shard_data_path(const std::filesystem::path& dir,
                                        std::string_view base,
                                        long offset) {
        std::ostringstream name;
        name << base << "."
            << std::setw(kShardPad) << std::setfill('0') << offset
            << ".ndjson";
        return dir / name.str();
    }

    std::filesystem::path shard_meta_path(const std::filesystem::path& dir,
                                        std::string_view base,
                                        long offset) {
        std::ostringstream name;
        name << base << "."
            << std::setw(kShardPad) << std::setfill('0') << offset
            << ".meta.json";
        return dir / name.str();
    }

    std::filesystem::path shard_error_path(const std::filesystem::path& errs_dir,
                                        std::string_view base,
                                        long offset) {
        std::ostringstream name;
        name << base << "."
            << std::setw(kShardPad) << std::setfill('0') << offset
            << ".error.json";
        return errs_dir / name.str();
    }

}