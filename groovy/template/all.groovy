def p = report.parameters
if (p == null) {
    p = [:]
}

def limit  = p.limit
def offset = p.offset
def target = p.target
def mode   = p.mode

if (target == null) target = "nodes"
target = target as String

// --- safe mode ---
def safe = (mode != null && mode == "safe")
def sm = p.safeMode
if (!safe && sm != null) {
    def s = sm.toString().trim().toLowerCase()
    if ((s == "true") || (s == "1") || (s == "yes")) {
        safe = true
    }
}

// --- validOnly mode ---
def validOnly = false
def vo = p.validOnly
if (vo == null) vo = p.valid_only
if (vo == null) vo = p.onlyValid
if (vo == null) vo = p.valid
if (vo != null) {
    def s = vo.toString().trim().toLowerCase()
    if ((s == "true") || (s == "1") || (s == "yes")) {
        validOnly = true
    }
}

def hasLimit = (limit != null)
def hasOffset = (offset != null)
if (hasLimit)  limit  = limit as int
if (hasOffset) offset = offset as int

if ((target == "node") || (target == "nodes") || (target == "entity") || (target == "entities") || (target == "ents")) {

    def citype = p.citype
    if (citype == null) citype = p.type   // allow "type" alias for nodes
    def hasCitype = (citype != null)
    if (hasCitype) citype = citype as String

    def qi_node = qi("node")

    def pth
    if (hasCitype) {
        def type_node = type(citype)
        pth = path().node(qi_node, type_node)
    } else {
        pth = path().node(qi_node)
    }

    def q = match(pth)

    // validOnly filter (nodes)
    if (validOnly) {
        q = q.where(not(qi_node.filter(state(StateEnum.INVALIDATED))))
    }

    if (safe) {
        q = q.returns(prop("uuid", qi.prop("\$cmdb_id")))
             .orderBy(qi_node.prop("\$cmdb_createdAt"), OrderDirection.ASC)
    } else {
        q = q.returns(node(qi_node))
             .orderBy(qi_node.prop("\$cmdb_createdAt"), OrderDirection.ASC)
    }

    if (hasLimit)  q = q.limit(limit)
    if (hasOffset) q = q.offset(offset)

    def res = Neo4j.execute(q)

    if (safe) {
        return res.data.collect { row ->
            [ uuid: row.uuid ]
        }
    } else {
        return res.data.collect { it.node }
    }

} else if ((target == "rel") || (target == "rels") || (target == "relation") || (target == "relations")) {

    // rel type can be in "reltype" or "type"
    def reltype = p.reltype
    if (reltype == null) reltype = p.relType
    if (reltype == null) reltype = p.type
    def hasReltype = (reltype != null)
    if (hasReltype) reltype = reltype as String

    // source type can be "sourceType" (and a couple common aliases)
    def sourceType = p.sourceType
    if (sourceType == null) sourceType = p.srcType
    if (sourceType == null) sourceType = p.source_type
    if (sourceType == null) sourceType = p.src
    def hasSourceType = (sourceType != null)
    if (hasSourceType) sourceType = sourceType as String

    // target type
    def targetType = p.targetType
    if (targetType == null) targetType = p.tgtType
    if (targetType == null) targetType = p.target_type
    if (targetType == null) targetType = p.tgt
    def hasTargetType = (targetType != null)
    if (hasTargetType) targetType = targetType as String

    // Always define QIs so we can always filter them in validOnly mode
    def qi_rel = qi("rel")
    def qi_src = qi("src")
    def qi_tgt = qi("tgt")

    // Build path for (src)-[rel]->(tgt) with optional types
    def pth = path()

    if (hasSourceType) {
        def type_SRC = type(sourceType)
        pth = pth.node(qi_src, type_SRC)
    } else {
        pth = pth.node(qi_src)
    }

    if (hasReltype) {
        def type_REL = type(reltype)
        pth = pth.rel(qi_rel, RelationshipDirection.OUT, type_REL)
    } else {
        pth = pth.rel(qi_rel, RelationshipDirection.OUT)
    }

    if (hasTargetType) {
        def type_TGT = type(targetType)
        pth = pth.node(qi_tgt, type_TGT)
    } else {
        pth = pth.node(qi_tgt)
    }

    // --- Build query in "base + match" style ---
    def base = match(path().node(qi_src))
    if (validOnly) {
        base = base.where(not(qi_src.filter(state(StateEnum.INVALIDATED))))
    }

    def q = base.match(pth)

    if (validOnly) {
        q = q.where(and(
            not(qi_rel.filter(state(StateEnum.INVALIDATED))),
            not(qi_tgt.filter(state(StateEnum.INVALIDATED)))
        ))
    }

    if (safe) {
        q = q.returns(prop("rel_uuid", qi_rel.prop("\$cmdb_id")))
             .orderBy(qi_rel.prop("\$cmdb_createdAt"), OrderDirection.ASC)
    } else {
        q = q.returns(rel(qi_rel))
             .orderBy(qi_rel.prop("\$cmdb_createdAt"), OrderDirection.ASC)
    }

    if (hasLimit)  q = q.limit(limit)
    if (hasOffset) q = q.offset(offset)

    def res = Neo4j.execute(q)

    if (safe) {
        return res.data.collect { row ->
            [ uuid: row.rel_uuid ]
        }
    } else {
        return res.data.collect { it.rel }
    }

} else {
    return [
        ok: false,
        error: "bad_target",
        message: "target must be 'nodes' or 'relations' (aliases: node/entity, rel/relation)",
        got: target
    ]
}
