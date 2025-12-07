def qi_target = qi("target")
def qi_source = qi("source")
def qi_rel    = qi("rel")

def type_TARGET = type("__TARGET__")
def type_SOURCE = type("__SOURCE__")
def type_REL    = type("__RELATION__")

// Base: all target nodes of the given type
def base = match(path().node(qi_target, type_TARGET))

// Join: target <-[REL]- source
def q = base.match(
    path()
        .node(qi_target)
        .rel(qi_rel, RelationshipDirection.IN, type_REL)
        .node(qi_source, type_SOURCE)
)

// No filters: we want ALL instances, regardless of state
def q2 = q.returns(rel(qi_rel)).limit(__LIMIT__).offset(__OFFSET__)
def res = Neo4j.execute(q2)
return res.data.collect { it.rel }