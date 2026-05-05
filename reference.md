---
title: Reference
nav_order: 3
has_children: true
permalink: /reference/
---

# Reference

Authoritative specs for the kernel's external surfaces — anything an integration needs to call, read, or write against.

- **HTTP API** — the `/feed`, `/signal`, `/marker`, `/tick`, `/ingest` contract.
- **MCP server** — the four retrieval tools agents call (`kb_search`, `kb_brief`, `brain_search_arcs`, `brain_get_arc`).
- **Vault schema** — the six-region Obsidian layout the kernel writes to.
- **Markers** — the `<!-- @lesson -->`, `<!-- @signal -->`, `<!-- @milestone -->`, `<!-- @decision -->` grammar.
- **Gateway contract** — what the kernel expects from your OpenAI-compatible LLM upstream.
