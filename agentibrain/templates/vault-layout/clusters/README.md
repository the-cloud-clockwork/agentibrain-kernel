# Clusters — Arc Storage

Canonical storage for brain arcs. Each arc is a markdown file with YAML frontmatter.

## Layout
- `clusters/<YYYY-MM-DD>/` — arcs grouped by extraction date
- `clusters/<YYYY-MM-DD>/_dashboard.md` — summary table for the day

## Lifecycle
1. brain-clusters skill extracts sessions → writes stub here
2. kb_brief synthesizes timeline/lessons/resolution via inference-gateway
3. brain-keeper computes heat + promotes hot arcs to frontal-lobe/conscious/
4. 7 days post-resolution → graduate to left/ or right/ by region
