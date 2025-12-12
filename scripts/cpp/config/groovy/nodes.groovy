def qi_node = qi("node")

def q = match(path().node(qi_node))
.returns(node(qi_node))
.orderBy(qi_node.prop("\$cmdb_createdAt"), OrderDirection.ASC)
.limit(__LIMIT__).offset(__OFFSET__)

def res = Neo4j.execute(q)
return res.data.collect { it.node }