#pragma once
#include "binary_formats.h"
#include <vector>
#include <fstream>
#include <algorithm>

namespace metais {

    class GlobalUuidIndex {
    public:
        void load(const fs::path& uuids_bin) {
            std::uint64_t bytes = fs::file_size(uuids_bin);
            if (bytes % 16 != 0) throw std::runtime_error("GlobalUuidIndex: uuids.bin not multiple of 16");
            std::size_t n = (std::size_t)(bytes / 16);

            uuids_.clear();
            uuids_.reserve(n);

            std::ifstream is(uuids_bin, std::ios::binary);
            if (!is) throw std::runtime_error("GlobalUuidIndex: open failed: " + uuids_bin.string());
            for (std::size_t i = 0; i < n; ++i) {
                uuids_.push_back(read_uuid_raw16(is));
            }
        }

        bool try_resolve(const Uuid128& u, GlobalId& out_gid) const {
            auto it = std::lower_bound(uuids_.begin(), uuids_.end(), u);
            if (it == uuids_.end() || !(*it == u)) return false;
            out_gid = (GlobalId)std::distance(uuids_.begin(), it);
            return true;
        }

        std::size_t size() const { return uuids_.size(); }

    private:
        std::vector<Uuid128> uuids_;
    };

}