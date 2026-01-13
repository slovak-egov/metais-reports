def qi_rel = qi("rel")

def q = match(
    path().node().rel(qi_rel, RelationshipDirection.IN).node()
)
.returns(prop("rel_uuid",  qi_rel.prop("\$cmdb_id")))
.orderBy(qi_rel.prop("\$cmdb_createdAt"), OrderDirection.ASC)
.limit(__LIMIT__).offset(__OFFSET__)

def res = Neo4j.execute(q)
return res.data.collect { row ->
    [
        uuid: row.rel_uuid
    ]
}