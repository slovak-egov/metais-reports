def flatten_node(node_obj: dict) -> dict:
    """
    Turn a MetaIS node object into one flat dict:

    {
      "uuid": ...,
      "type": ...,
      ...attributes...,
      ...metaAttributes...
    }
    """
    flat = {}
    flat["uuid"] = node_obj.get("uuid")
    flat["type"] = node_obj.get("type")

    for a in node_obj.get("attributes", []) or []:
        name = a.get("name")
        if not name:
            continue
        flat[name] = a.get("value")

    meta = node_obj.get("metaAttributes") or {}
    for k, v in meta.items():
        flat[k] = v

    return flat