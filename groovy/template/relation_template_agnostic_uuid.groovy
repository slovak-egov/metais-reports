def qi_rel = qi("rel")
def qi_src = qi("src")
def qi_tgt = qi("tgt")

def q = match(
    path().node(qi_src).rel(qi_rel).node(qi_tgt)
)
.returns(
    prop("source", qi_src.prop("\$cmdb_id")),
    prop("target", qi_tgt.prop("\$cmdb_id")),
    prop("sourceType", qi_src.prop("\$cmdb_typeName")),
    prop("targetType", qi_tgt.prop("\$cmdb_typeName"))
)
.orderBy(qi_rel.prop("\$cmdb_createdAt"), OrderDirection.ASC)
.limit(__LIMIT__).offset(__OFFSET__)

def res = Neo4j.execute(q)

return res.data.collect { row ->
    [
        source: [
            uuid: row.source,
            type: row.sourceType,
        ],
        target: [
            uuid: row.target,
            type: row.targetType,
        ]
    ]
}