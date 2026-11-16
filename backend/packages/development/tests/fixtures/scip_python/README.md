# SCIP fixture provenance

`index.scip` is the unmodified output of `@sourcegraph/scip-python` 0.6.8 (MIT), executed with Node.js 22.23.5 on the two synthetic source files in this directory and an empty environment list:

```
scip-python index --cwd <fixture-dir> --project-name synthetic-demo --project-version 1.0 --environment <empty-list.json> --output index.scip --quiet
```

The indexer's `project_root` records the temporary generation path; the consumer never opens or returns it. The indexer omits `Document.position_encoding`. The consumer accepts its positions only for ASCII content, where UTF-8, UTF-16 and UTF-32 unit offsets coincide, and marks coverage PARTIAL. This fixture does not establish behavior for non-ASCII output from this indexer.
