def headers = [
        new Header("central_uuid", Header.Type.STRING),
        new Header("outer_uuid", Header.Type.STRING)
    ]
 
 def qi_central = qi("central")
 def qi_rel = qi("rel")
 def qi_outer = qi("outer")
 
 def type_CENTRAL = type("__CENTRAL__")
 def type_OUTER = type("__OUTER__")
 def type_REL = type("__RELATION__")
 
 def base = match(path().node(qi_central, type_CENTRAL))
 .where(not(qi_central.filter(state(StateEnum.INVALIDATED))))
 
 def q = base.match(
    path()
    .node(qi_central)
    .rel(qi_rel, RelationshipDirection.IN, type_REL)
    .node(qi_outer, type_OUTER)
    )
    .where(and(
    not(qi_rel.filter(state(StateEnum.INVALIDATED))),
    not(qi_outer.filter(state(StateEnum.INVALIDATED)))
    ))
 
 def resCount = Neo4j.execute(q.returns(count("totalCount", qi_central)))
 def total = (resCount.data && resCount.data[0]?.totalCount) ? (resCount.data[0].totalCount as int) : 0
 
 def query = q.returns(
        prop("central_uuid", qi_central.prop("\$cmdb_id")),
        prop("outer_uuid", qi_outer.prop("\$cmdb_id"))
    )
    .orderBy(qi_central.prop("\$cmdb_id"), OrderDirection.ASC)
 
 def res = Neo4j.execute(query)
 
 def table = new Report(headers)
 for (row in res.data) {
    table.add([ row.central_uuid, row.outer_uuid ])
 }
 
 def result = new ReportResult("TABLE", table, total)
 return result