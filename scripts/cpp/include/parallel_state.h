#pragma once
#include <filesystem>
#include <string>

namespace metais {

    long claim_next_offset(const std::filesystem::path& state_dir, int page_size);

    void set_stop_at(const std::filesystem::path& state_dir, long stop_at);
    bool get_stop_at(const std::filesystem::path& state_dir, long& out_stop_at);

    void write_shared_token(const std::filesystem::path& state_dir, const std::string& token);
    std::string read_shared_token(const std::filesystem::path& state_dir);

    void record_failed_offset(const std::filesystem::path& state_dir, long offset);
    bool read_and_clear_min_failed_offset(const std::filesystem::path& state_dir, long& out_min);
    long read_next_offset(const std::filesystem::path& state_dir, long def = 0);
    void write_next_offset_if_smaller(const std::filesystem::path& state_dir, long candidate);
}