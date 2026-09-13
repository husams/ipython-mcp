# Codex benchmark

| model | condition | repeat | status | total tokens | correctness |
|---|---|---:|---|---:|---|
| gpt-5.6-luna | full | 1 | success | 181157 | True |
| gpt-5.6-luna | compact | 1 | success | 229389 | True |
| gpt-6-astra | full | 1 | success | 159153 | True |
| gpt-6-astra | compact | 1 | success | 164704 | True |

## Paired savings

| model | repeat | full tokens | compact tokens | saved | saved % |
|---|---:|---:|---:|---:|---:|
| gpt-5.6-luna | 1 | 181157 | 229389 | -48232 | -26.62% |
| gpt-6-astra | 1 | 159153 | 164704 | -5551 | -3.49% |

Token savings include only paired successful full/compact cases. A single repeat is subject to normal prompt and cache effects.
