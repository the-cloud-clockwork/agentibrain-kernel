-- Rollback for SV2-GRF-01: drops only the graph projection tables.
DROP TABLE IF EXISTS graph_relation_quarantine;
DROP TABLE IF EXISTS graph_relations;
DROP TABLE IF EXISTS graph_entities;
DROP TABLE IF EXISTS graph_relation_pairs;
DROP TABLE IF EXISTS graph_relation_types;
DROP TABLE IF EXISTS graph_entity_kinds;
