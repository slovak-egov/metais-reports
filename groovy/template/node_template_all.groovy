def qi_node   = qi("node")
def type_node = type("__TYPE__")

def q = match(path().node(qi_node, type_node))

def q2 = q.returns(node(qi_node)).limit(__LIMIT__).offset(__OFFSET__)
def res = Neo4j.execute(q2)
return res.data.collect { it.node }