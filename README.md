# Dementia Bench

A 29-task Harbor package of synthetic memory-clinic items. Each task asks for one structured JSON answer. The hidden scoring key is in `tasks/<id>/tests/` and is not in the prompt.

This release keeps the cases GPT-6-Astra approved on 6 November 2026 and leaves out the 31 it flagged. The research note is in [`docs/index.html`](docs/index.html). Not medical advice. Not a physician-confirmed label.

| Task | Question | Cases |
|---|---|---:|
| T1 | Differential | 6 |
| T2 | Two-year change | 15 |
| T3 | A/T/N call | 6 |
| T4 | Co-pathology | 0 |
| T5 | Next test | 2 |

No co-pathology item survived review, so T4 is absent.

## Run

From this directory, with [Harbor](https://www.harborframework.com/) installed and `XAI_API_KEY` already exported if you use the bundled Grok grader:

```bash
export HARBOR_TELEMETRY=off
harbor run \
  -p tasks \
  -a terminus-2 \
  -m xai/grok-4.7 \
  --ak api_base=https://api.x.ai/v1 \
  --ak llm_backend=litellm \
  -n 1 \
  -k 1 \
  -o /tmp/dementia-bench-jobs
```

The agent phase has no network. The verifier container can reach the rubric judge. The key is read from the environment. It is not stored in this repository.

`acceptance.json` is the release list. `exclusions.json` is the reason each omitted case was left out. The omitted vignettes are not in this repository.

## Layout

- `cases/<id>/` is the vignette, the full answer file, and the draft rubric.
- `tasks/<id>/` is the Harbor task. `instruction.md` is what the agent sees. `tests/answer.json` and `tests/rubric.json` are for the verifier.

Package names in `task.toml` are `dementia-bench/<case id>`.
