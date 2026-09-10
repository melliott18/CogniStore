# Supervised baseline fixture

`dataset-v1.json` is a synthetic v1 dataset generated through the real snapshot
capture and privacy-filtered export functions. It contains 60 independent moves:
20 each in the train, validation, and test windows of
`configs/policy-baseline-v1.json`. Each partition has 10 successes and 10 failures
and recorded simple/content rule cohorts. Size deliberately separates the labels
so numerical results are easy to verify. This is test evidence, not a measured
placement improvement or a dataset suitable for production promotion.

Regenerate from the repository root with:

```bash
python -m tests.fixtures.policy_baseline.generate
```

The IDs and timestamps are fixed, the export omits object coordinates, and there
is no network, storage-driver, or persistent-catalog access. Training and
evaluation examples in `docs/policy_baseline.md` can use this dataset as `--input`.
