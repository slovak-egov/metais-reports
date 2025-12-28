#pragma once
#include "directory_layout.h"

namespace metais {
    
    // finalize relation edges from tmp.edges.bin into per-(SRC,TGT) adjacency files.
    void pass3_finalize_relation_edges(const DirectoryLayout& layout);

}