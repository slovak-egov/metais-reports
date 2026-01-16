#pragma once
#include "directory_layout.h"

namespace metais {

    void pass2_pack_nodes_and_relations(const DirectoryLayout& layout, bool skip_bad_json);
    
}