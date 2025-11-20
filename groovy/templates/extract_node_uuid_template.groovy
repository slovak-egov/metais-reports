def qi_node   = qi("node")
def type_node = type("__TYPE__")

def q = match(path().node(qi_node, type_node))
  .where(not(qi_node.filter(state(StateEnum.INVALIDATED))))

def res = Neo4j.execute(
  q.returns(prop("uuid", qi_node.prop("\$cmdb_id")))
)
return res.data.collect { it.uuid }