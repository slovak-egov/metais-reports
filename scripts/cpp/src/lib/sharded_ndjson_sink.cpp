#include "sharded_ndjson_sink.h"

#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <chrono>
#include <ctime>

namespace fs = std::filesystem;

using metais::kShardPad;

namespace {

    static fs::path shard_path(
        const fs::path& dir,
        const std::string& base,
        long offset
    ) {
        std::ostringstream name;
        name << base << "."
            << std::setw(kShardPad) << std::setfill('0') << offset
            << ".ndjson";
        return dir / name.str();
    }

    static fs::path shard_meta_path(const fs::path& dir, const std::string& base, long offset) {
        std::ostringstream name;
        name << base << "."
            << std::setw(kShardPad) << std::setfill('0') << offset
            << ".meta.json";
        return dir / name.str();
    }

    inline std::string now_iso8601_local() {
        using namespace std::chrono;
        auto now = system_clock::now();
        std::time_t t = system_clock::to_time_t(now);

        std::tm tm{};
        localtime_r(&t, &tm);

        std::ostringstream oss;
        oss << std::put_time(&tm, "%Y-%m-%dT%H:%M:%S%z"); // +0100
        std::string s = oss.str();
        // If you want "+01:00" insert colon before last two digits.
        if (s.size() >= 5) s.insert(s.size()-2, ":");
        return s;
    }

}

namespace metais {

    ShardedNdjsonSink::ShardedNdjsonSink(
        fs::path pages_dir,
        std::string base_name
    )
        : pages_dir_(std::move(pages_dir)),
        base_name_(std::move(base_name))
    {
        fs::create_directories(pages_dir_);
    }

    void ShardedNdjsonSink::begin_page(long offset, int) {
        current_offset_ = offset;

        final_path_ = shard_path(pages_dir_, base_name_, offset);
        auto meta_final = shard_meta_path(pages_dir_, base_name_, offset);
        tmp_path_   = final_path_;
        tmp_path_  += ".tmp";

        const bool has_data = fs::exists(final_path_);
        const bool has_meta = fs::exists(meta_final);

        if (fs::exists(tmp_path_)) fs::remove(tmp_path_);

        // Idempotency: if page already exists, skip silently
        if (has_data && has_meta) { current_offset_ = -1; return; }
        if (has_data && !has_meta) { fs::remove(final_path_); } 
        if (!has_data && has_meta) { fs::remove(meta_final); }

        out_.open(tmp_path_, std::ios::binary);
        if (!out_) {
            throw std::runtime_error("Cannot open " + tmp_path_.string());
        }
    }

    void ShardedNdjsonSink::write_item(const nlohmann::json& obj) {
        if (current_offset_ < 0) return; // skipped page
        out_ << obj.dump() << "\n";
    }

    void ShardedNdjsonSink::end_page(const PageStats& stats) {
        if (current_offset_ < 0) return;

        out_.close();
        fs::rename(tmp_path_, final_path_);

        // Write meta (atomic)
        const fs::path meta_final = shard_meta_path(pages_dir_, base_name_, current_offset_);
        fs::path meta_tmp = meta_final;
        meta_tmp += ".tmp";
        if (fs::exists(meta_tmp)) fs::remove(meta_tmp);

        nlohmann::json meta;
        meta["offset"]    = stats.offset;
        meta["limit"]     = stats.limit;
        meta["received"]  = stats.received;
        meta["seconds"]   = stats.seconds;
        meta["timestamp"] = now_iso8601_local();

        {
            std::ofstream m(meta_tmp, std::ios::binary);
            if (!m) throw std::runtime_error("Cannot open " + meta_tmp.string());
            m << meta.dump(2);
        }
        fs::rename(meta_tmp, meta_final);

        current_offset_ = -1;
    }

}
