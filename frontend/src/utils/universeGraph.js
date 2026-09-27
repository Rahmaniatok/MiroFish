// Builds a GraphPanel-compatible {nodes, edges} graph from a locked
// ticker_universe (step 1 artifact), so the workspace has a graph to show
// before the Zep knowledge graph exists (step 3 replaces it).

export const formatMarketCap = (cap) => {
  if (cap == null) return '—'
  if (cap >= 1e12) return `$${(cap / 1e12).toFixed(2)}T`
  if (cap >= 1e9) return `$${(cap / 1e9).toFixed(1)}B`
  if (cap >= 1e6) return `$${(cap / 1e6).toFixed(1)}M`
  return `$${Math.round(cap)}`
}

export function universeToGraphData(universe) {
  if (!universe?.universe?.length) return null
  const nodes = []
  const edges = []
  const sectorIds = {}
  const tierIds = {}

  const sectorNode = (sector) => {
    if (!sectorIds[sector]) {
      sectorIds[sector] = `sector::${sector}`
      nodes.push({ uuid: sectorIds[sector], name: sector, labels: ['Entity', 'Sector'], attributes: {}, summary: 'GICS sector' })
    }
    return sectorIds[sector]
  }
  const tierNode = (tier) => {
    if (!tierIds[tier]) {
      tierIds[tier] = `tier::${tier}`
      nodes.push({ uuid: tierIds[tier], name: `${tier.toUpperCase()} CAP`, labels: ['Entity', 'MarketCapTier'], attributes: {}, summary: '' })
    }
    return tierIds[tier]
  }

  universe.universe.forEach((row) => {
    const id = `ticker::${row.ticker}`
    nodes.push({
      uuid: id,
      name: row.ticker,
      labels: ['Entity', 'Company'],
      attributes: {
        company_name: row.company_name,
        market_cap: formatMarketCap(row.market_cap),
        market_cap_tier: row.market_cap_tier,
        as_of_date: universe.as_of_date
      },
      summary: `${row.company_name} — ${row.gics_sector}, ${formatMarketCap(row.market_cap)}`
    })
    edges.push({
      uuid: `${id}->sector`, source_node_uuid: id, target_node_uuid: sectorNode(row.gics_sector),
      name: 'IN_SECTOR', fact_type: 'IN_SECTOR', fact: `${row.ticker} is in ${row.gics_sector}`
    })
    edges.push({
      uuid: `${id}->tier`, source_node_uuid: id, target_node_uuid: tierNode(row.market_cap_tier),
      name: 'CAP_TIER', fact_type: 'CAP_TIER', fact: `${row.ticker} is ${row.market_cap_tier} cap`
    })
  })
  return { nodes, edges, node_count: nodes.length, edge_count: edges.length }
}
