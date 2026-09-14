"""Schema shape that both the ORM models and the Alembic migration must agree on.

Read from the environment rather than Settings so Alembic and unit tests can
import the models without a full application config.
"""

import os

#: Width of the `chunks.embedding` column. Must equal Embedder.dimensions;
#: app/registry.py asserts this at startup.
EMBEDDING_DIMENSIONS: int = int(os.getenv("EMBEDDING_DIMENSIONS", "768"))

#: Postgres text-search configuration used for the generated `tsv` column.
#: A language choice, not a business domain -- swapping it needs a migration.
FTS_LANGUAGE: str = os.getenv("FTS_LANGUAGE", "english")

#: SQL for the generated tsvector. Header is weighted above body text so a
#: section title match outranks an incidental body mention.
TSV_EXPRESSION: str = (
    f"setweight(to_tsvector('{FTS_LANGUAGE}', coalesce(contextual_header, '')), 'A') "
    f"|| setweight(to_tsvector('{FTS_LANGUAGE}', coalesce(content, '')), 'B')"
)

#: HNSW graph parameters for the `chunks.embedding` index.
#:
#: HNSW rather than IVFFlat because IVFFlat trains its centroids when the index
#: is built -- and the migration builds it on an empty table. An untrained
#: IVFFlat index does not merely lose recall, it returns *no rows*, which this
#: product would surface as "the documents do not cover that": a confident,
#: wrong refusal. HNSW needs no training step, so it is correct from the first
#: row and stays correct as the corpus grows.
#:
#: `m` is connections per node, `ef_construction` the build-time candidate list.
#: Higher means better recall, slower builds and more memory. 16/64 are the
#: pgvector defaults and are fine well past this PoC's scale.
HNSW_M: int = int(os.getenv("HNSW_M", "16"))
HNSW_EF_CONSTRUCTION: int = int(os.getenv("HNSW_EF_CONSTRUCTION", "64"))
