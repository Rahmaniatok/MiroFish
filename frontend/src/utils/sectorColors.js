// One fixed colour per GICS sector, shared by the final-list table and the company map.
export const SECTOR_COLORS = {
  'Industrials': '#64748B',
  'Information Technology': '#7C3AED',
  'Financials': '#2563EB',
  'Utilities': '#4338CA',
  'Health Care': '#06B6D4',
  'Materials': '#78350F',
  'Consumer Discretionary': '#F59E0B',
  'Consumer Staples': '#22C55E',
  'Real Estate': '#EF4444',
  'Communication Services': '#A21CAF',
  'Energy': '#0F766E'
}

export const sectorColor = (sector) => SECTOR_COLORS[sector] || '#9CA3AF'

// Cluster backgrounds: soft, distinct from the sector dots drawn on top of them.
export const CLUSTER_COLORS = [
  '#F97316', '#3B82F6', '#10B981', '#E11D48', '#8B5CF6', '#EAB308',
  '#14B8A6', '#EC4899', '#84CC16', '#6366F1', '#F43F5E', '#0EA5E9'
]
export const clusterColor = (id) => id == null ? '#CBD5E1' : CLUSTER_COLORS[id % CLUSTER_COLORS.length]
