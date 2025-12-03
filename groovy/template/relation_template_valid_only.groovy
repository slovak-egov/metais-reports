def qi_target = qi("target")
def qi_source = qi("source")
def qi_rel    = qi("rel")

def type_TARGET = type("__TARGET__")
def type_SOURCE = type("__SOURCE__")
def type_REL    = type("__RELATION__")

def base = match(path().node(qi_target, type_TARGET))
    .where(not(qi_target.filter(state(StateEnum.INVALIDATED))))

def q = base.match(
    path()
        .node(qi_target)
        .rel(qi_rel, RelationshipDirection.IN, type_REL)
        .node(qi_source, type_SOURCE)
).where(and(
    not(qi_rel.filter(state(StateEnum.INVALIDATED))),
    not(qi_source.filter(state(StateEnum.INVALIDATED)))
))

def query = q.returns(
    prop("source", qi_source.prop("\$cmdb_id")),
    prop("target", qi_target.prop("\$cmdb_id"))
).limit(__LIMIT__).offset(__OFFSET__)

def res = Neo4j.execute(query)

return res.data.collect { row ->
    [ source: row.source, target: row.target ]
}