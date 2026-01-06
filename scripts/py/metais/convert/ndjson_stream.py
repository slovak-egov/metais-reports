from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from metais.common.shards import (
    ShardInfo,
    list_shards_by_meta,
)


@dataclass(frozen=True)
class NdjsonJsonRecord:
    obj: Any
    shard_offset: int
    line_no: int
    shard_index: int
    shard_count: int


def ndjson_json_range(
    pages_dir: Path,
    base: str,
    *,
    skip_bad_json: bool = False,
) -> Iterator[NdjsonJsonRecord]:
    """
    Streaming NDJSON reader over shards identified by *.meta.json.

    Yields NdjsonJsonRecord with:
      - obj parsed JSON
      - shard_offset (from filename)
      - shard_index/shard_count
      - line_no (1-based within shard file)
    """
    shards = list_shards_by_meta(Path(pages_dir), str(base))

    for shard_i, shard in enumerate(shards):
        shard_count = len(shards)

        # Text mode is fine because writer always wrote UTF-8.
        with open(shard.ndjson_path, "r", encoding="utf-8") as f:
            line_no = 0
            for line in f:
                line_no += 1
                if not line:
                    continue
                line = line.strip()
                if not line:
                    continue

                try:
                    obj = json.loads(line)
                except Exception as e:
                    if skip_bad_json:
                        continue
                    preview = line[:400] + ("..." if len(line) > 400 else "")
                    raise RuntimeError(
                        "[ndjson:%s] invalid JSON line\n"
                        "  shard_index=%d/%d\n"
                        "  shard_offset=%d\n"
                        "  line_no=%d\n"
                        "  file=%s\n"
                        "  error=%s\n"
                        "  line_preview=%s\n"
                        % (base, shard_i, shard_count, shard.offset, line_no, shard.ndjson_path, e, preview)
                    ) from e

                yield NdjsonJsonRecord(
                    obj=obj,
                    shard_offset=shard.offset,
                    line_no=line_no,
                    shard_index=shard_i,
                    shard_count=shard_count,
                )