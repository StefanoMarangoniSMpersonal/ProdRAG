"""Retrieval + answer evaluation for ProdRAG.

Q4 lands the retrieval half: pure ranking metrics (`metrics.py`), a golden-set builder
(`build_golden.py`), and a harness that runs the golden questions through `retrieve()`
and records hit@k / MRR (`run.py`). RAGAS answer-quality metrics arrive at Q9 in this
same package.

The package lives at the REPO ROOT (not under backend/) so its CLI entrypoint reads
`python -m eval.run` — the interface the `eval-run` skill already expects. It imports the
app (`app.retrieve...`) across the root boundary; `pytest.ini`'s `pythonpath` and the
CLIs' own `sys.path` bootstrap make that resolve from either root.
"""
