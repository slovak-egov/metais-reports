#include "../include/adaptive_pager.h"
#include "../include/json_utils.h"  // for load_json_file (nlohmann)
#include <algorithm>
#include <iostream>

namespace metais {

    // -------- AdaptivePager --------

    AdaptivePager::AdaptivePager(int initial_limit, PagerPolicy policy)
        : limit_(initial_limit), policy_(std::move(policy)) {
        clamp_and_quantize();
    }

    void AdaptivePager::on_success(double seconds) {
        // grow if fast
        if (seconds >= 0.0 && seconds < policy_.grow_if_under_seconds) {
            limit_ = int(limit_ * policy_.grow_factor);
            clamp_and_quantize();
            return;
        }

        // shrink if slow
        if (seconds > policy_.shrink_if_over_seconds) {
            limit_ = int(limit_ * policy_.shrink_factor);
            clamp_and_quantize();
            return;
        }

        // otherwise keep as-is
    }

    void AdaptivePager::on_timeout_like() {
        limit_ = int(limit_ * policy_.timeout_factor);
        clamp_and_quantize();
    }

    void AdaptivePager::clamp_and_quantize() {
        // clamp
        if (limit_ < policy_.min_limit) limit_ = policy_.min_limit;
        if (limit_ > policy_.max_limit) limit_ = policy_.max_limit;

        // quantize
        int step = policy_.quantize_step;
        if (step > 1) {
            // round to nearest multiple of step (never below 1)
            int q = (limit_ + step / 2) / step;
            limit_ = std::max(1, q * step);

            // re-clamp after quantization
            if (limit_ < policy_.min_limit) limit_ = policy_.min_limit;
            if (limit_ > policy_.max_limit) limit_ = policy_.max_limit;
        }
    }

    // -------- JSON loader --------

    PagerPolicy load_pager_policy(const std::filesystem::path& path) {
        PagerPolicy p;

        if (path.empty()) return p;
        if (!std::filesystem::exists(path)) {
            std::cerr << "[pager] policy file not found: " << path << " (using defaults)\n";
            return p;
        }

        try {
            auto j = load_json_file(path.string());

            auto get_i = [&](const char* k, int& dst) {
                auto it = j.find(k);
                if (it != j.end() && it->is_number_integer()) dst = it->get<int>();
            };
            auto get_d = [&](const char* k, double& dst) {
                auto it = j.find(k);
                if (it != j.end() && it->is_number()) dst = it->get<double>();
            };

            get_i("min_limit", p.min_limit);
            get_i("max_limit", p.max_limit);

            get_d("grow_if_under_seconds", p.grow_if_under_seconds);
            get_d("grow_factor",           p.grow_factor);

            get_d("shrink_if_over_seconds", p.shrink_if_over_seconds);
            get_d("shrink_factor",          p.shrink_factor);

            get_d("timeout_factor",         p.timeout_factor);

            get_i("quantize_step",          p.quantize_step);

            // sanity (avoid footguns)
            if (p.min_limit < 1) p.min_limit = 1;
            if (p.max_limit < p.min_limit) p.max_limit = p.min_limit;

            if (p.grow_factor < 1.0)  p.grow_factor = 1.0;
            if (p.shrink_factor <= 0.0 || p.shrink_factor >= 1.0) p.shrink_factor = 0.9;
            if (p.timeout_factor <= 0.0 || p.timeout_factor >= 1.0) p.timeout_factor = 0.5;

            if (p.quantize_step < 1) p.quantize_step = 1;

            return p;
        } catch (const std::exception& e) {
            std::cerr << "[pager] failed to load policy " << path
                    << ": " << e.what() << " (using defaults)\n";
            return p;
        }
    }

}