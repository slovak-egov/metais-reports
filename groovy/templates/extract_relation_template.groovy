def headers = [
        new Header("source_uuid", Header.Type.STRING),
        new Header("target_uuid", Header.Type.STRING)
    ]
 
 def qi_target = qi("central")
 def qi_rel = qi("rel")
 def qi_source = qi("outer")
 
 def type_TARGET = type("__CENTRAL__")
 def type_SOURCE = type("__OUTER__")
 def type_REL = type("__RELATION__")
 
 def base = match(path().node(qi_target, type_TARGET))
 
 def q = base.match(
    path()
    .node(qi_target)
    .rel(qi_rel, RelationshipDirection.IN, type_REL)
    .node(qi_source, type_SOURCE)
    )
 
 def resCount = Neo4j.execute(q.returns(count("totalCount", qi_target)))
 def total = (resCount.data && resCount.data[0]?.totalCount) ? (resCount.data[0].totalCount as int) : 0
 
 def query = q.returns(
        prop("source_uuid", qi_source.prop("\$cmdb_id")),
        prop("target_uuid", qi_target.prop("\$cmdb_id"))
    )
 
 def res = Neo4j.execute(query)
 
 def table = new Report(headers)
 for (row in res.data) {
    table.add([ row.central_uuid, row.outer_uuid ])
 }
 
 def result = new ReportResult("TABLE", table, total)
 return result