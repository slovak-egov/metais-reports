#include "adaptive_pager.h"

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

}