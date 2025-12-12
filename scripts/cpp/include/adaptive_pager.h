#pragma once
#include "pager_policy.h"

namespace metais {

    class AdaptivePager {
    public:
        AdaptivePager(int initial_limit, PagerPolicy policy);

        int limit() const { return limit_; }

        // Call after a successful HTTP 200 response.
        void on_success(double seconds);

        // Call on timeout-like conditions (HTTP 408/504/etc or curl timeout).
        void on_timeout_like();

    private:
        void clamp_and_quantize();

        int limit_;
        PagerPolicy policy_;
    };

}