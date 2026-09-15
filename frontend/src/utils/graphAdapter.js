/**
 * Adapts Phase 2b/2c's FilteredEntities shape (entities with per-node
 * related_edges, as returned by GET /api/portfolio/graph) into the flat
 * {nodes, edges} shape GraphPanel.vue expects (originally the shape of
 * GraphBuilderService.get_graph_data() for a Zep graph).
 *
 * Each logical edge is recorded TWICE in the source data — once as
 * "outgoing" on its source entity, once as "incoming" on its target entity,
 * same `fact` string both times (Phase 2c). Keeping only the outgoing half
 * yields each edge exactly once.
 */
export function seedToGraphData(seed) {
  const entities = seed?.entities || []

  const nodes = entities.map(e => ({
    uuid: e.uuid,
    name: e.name,
    labels: e.labels,
    summary: e.summary,
    attributes: e.attributes
  }))

  const edges = []
  for (const entity of entities) {
    for (const edge of entity.related_edges || []) {
      if (edge.direction !== 'outgoing') continue
      edges.push({
        source_node_uuid: entity.uuid,
        target_node_uuid: edge.target_node_uuid,
        name: edge.edge_name,
        fact_type: edge.edge_name,
        fact: edge.fact
      })
    }
  }

  return { nodes, edges }
}
