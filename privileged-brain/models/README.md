# Privileged Brain — Model Checkpoints

Each subfolder is a LoRA adapter (SFT or DPO) saved after a training run.
All adapters target `Qwen/Qwen2.5-Coder-1.5B-Instruct` as the base model.

| Folder | Type | Dataset | GPU | Token Acc | FEH | Notes |
|---|---|---|---|---|---|---|
| `run3_nl2bash_l4_sft` | SFT | NL2Bash (10,512) + synthetic (616) | L4 | 92.6% | 27.5% | Clean baseline; eliminated markdown fences; curl\|bash REFUSE regression |

## Naming Convention

```
run{N}_{dataset}_{gpu}_{type}
```

- `N` — run number (increment each training run)
- `dataset` — primary data source (`nl2bash`, `alfa`, `beginner`, etc.)
- `gpu` — hardware (`l4`, `t4`, `a100`, `m4`)
- `type` — `sft` or `dpo`
