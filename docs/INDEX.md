# Documentation Index

> Single point of truth for all documentation in the Icebreaker repo.
> Every `.md` file in the project is listed here with its purpose, owner,
> and current status. If a document is not in this index, it does not exist.
>
> Last updated: June 2026 (Phase 6 complete, 1446 tests, Phases 0–6 shipped).

---

## Active Documents

These are the living documents that govern the project. They are kept
current and should be updated whenever the project state changes.

### Root-Level

| File | Purpose | Owner |
|---|---|---|
| [`AI_Native_OS_Whitepaper.md`](../AI_Native_OS_Whitepaper.md) | Definitive architecture reference. Design decisions, KPIs, threat model, the dual-brain pipeline, all eight invariants (INV-1–INV-8). **Read this first.** | Project lead |
| [`CLAUDE.md`](../CLAUDE.md) | AI agent instructions. Architectural invariants, security-critical file list, forbidden patterns, engineering best practices (BP-1–BP-12), agent workflow rules (WF-1–WF-7), test-only knobs, latency budget. Loaded automatically by Claude Code at session start. | Project lead |
| [`README.md`](../README.md) | Project overview. Architecture diagram, phase status table, directory map, key documents table, quick-start commands, security model summary. The public-facing entry point. | Project lead |

### `docs/`

| File | Purpose | Owner |
|---|---|---|
| [`docs/INDEX.md`](INDEX.md) | **This file.** Master index of all documentation. | Project lead |
| [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) | Architecture deep-dive. Component hierarchy, data flow, the fine-tuning pipeline, security layers, phase status. More detailed than the README, less prescriptive than the whitepaper. | Project lead |
| [`docs/implementation_plan.md`](implementation_plan.md) | Master build plan. Phase status table (§0), close-out notes for Phases 2–6, go/no-go gate checklists, milestone definitions, risk register, failure modes. The operational playbook. | Project lead |

### `cx-distro/`

| File | Purpose | Owner |
|---|---|---|
| [`cx-distro/README.md`](../cx-distro/README.md) | ISO builder module guide. Prerequisites, quick-start Docker commands, fast iteration flags (`--skip-to`, `--no-models`), pin files, 6-stage build table. | DevOps engineer |

### `dual-brain/`

| File | Purpose | Owner |
|---|---|---|
| [`dual-brain/README.md`](../dual-brain/README.md) | Controller module guide. Deploy workflow (`tar → scp → extract`), directory layout, how to run the Controller in dev mode, dependency list. | Backend engineer |
| [`dual-brain/dbtests/README.md`](../dual-brain/dbtests/README.md) | Manual dual-brain test harness. Five-step verification procedure for end-to-end QB→PB→mcpd dispatch, pass/fail criteria, environment setup. | Backend engineer |
| [`dual-brain/dbtests/what_to_look_for.md`](../dual-brain/dbtests/what_to_look_for.md) | Red-flag cheatsheet. Symptom → cause → fix table for common test failures: schema mismatches, model refusals, transport errors, sandbox violations. | Backend engineer |

### `privileged-brain/`

| File | Purpose | Owner |
|---|---|---|
| [`privileged-brain/README.md`](../privileged-brain/README.md) | Fine-tuning pipeline guide. 7-step training workflow (setup → data → synthetic → train → convert → inference → evaluate), time estimates, checkpoint resume instructions. | ML engineer |
| [`privileged-brain/colab/COLAB_INSTRUCTIONS.md`](../privileged-brain/colab/COLAB_INSTRUCTIONS.md) | Google Colab training guide. How to upload data, run the SFT notebook, handle checkpoints, download adapters. For running training without a local GPU. | ML engineer |
| [`privileged-brain/gcloud/GCLOUD_INSTRUCTIONS.md`](../privileged-brain/gcloud/GCLOUD_INSTRUCTIONS.md) | GCP VM training guide. VM creation, SSH setup, training execution, cost estimates, adapter download workflow. For L4/T4 GPU training runs. | ML engineer |
| [`privileged-brain/models/README.md`](../privileged-brain/models/README.md) | Model checkpoint registry. Naming convention, run3 baseline metrics (92.6% token accuracy, 27.5% FEH), model file locations. | ML engineer |
| [`privileged-brain/training/adapters/sft/README.md`](../privileged-brain/training/adapters/sft/README.md) | Auto-generated TRL model card for the current SFT adapter. Created by the training pipeline; describes base model, training config, and framework versions. | Auto-generated |

---

## Archived Documents

Historical documents, completed phase plans, one-time notes, and reference
materials that no longer require updates. Preserved in `docs/Archive/` for
provenance. Organized by category.

### Session Handoffs & Onboarding

| File | Summary |
|---|---|
| [`docs/Archive/HANDOFF.md`](Archive/HANDOFF.md) | Session handoff document for onboarding a new Mac. Conversation history, memory recreation instructions, lessons learned from prior sessions. |
| [`docs/Archive/NEW_MAC_ONBOARDING.md`](Archive/NEW_MAC_ONBOARDING.md) | One-time onboarding script. Part A: paste into Claude Code on fresh machine. Part B: memory files to recreate. Used once, then archived. |

### Phase Plans & Roadmaps (Completed)

| File | Phase | Summary |
|---|---|---|
| [`docs/Archive/phase1_roadmap.md`](Archive/phase1_roadmap.md) | Phase 1 | mcpd Rust daemon roadmap. M1.0–M1.10 milestones, Landlock/Seccomp/COW integration. |
| [`docs/Archive/phase2_roadmap_CLOSED_2026-06-11.md`](Archive/phase2_roadmap_CLOSED_2026-06-11.md) | Phase 2 | Dual-Brain Controller roadmap. M2.0–M2.14, exit gates G1–G11, VM validation, explicitly marked CLOSED. |
| [`docs/Archive/2026-06-05_plan_summary.md`](Archive/2026-06-05_plan_summary.md) | Phase 2 | Layman's-terms summary of the Phase 2 design (from `docs/phase2/`). |
| [`docs/Archive/findings.md`](Archive/findings.md) | Phase 2 | Research findings from the Phase 2 planning round (from `docs/phase2/`). |
| [`docs/Archive/m25_production_migration.md`](Archive/m25_production_migration.md) | Phase 2→6 | Dev-time to production path translation table. Maps M2.5 dev paths to Phase 6 FHS distro paths. |
| [`docs/Archive/V1_ROADMAP.md`](Archive/V1_ROADMAP.md) | Phase 4 | Privileged Brain V1 roadmap. Run-6 metrics, FEH regression analysis, Run-7 resolution. Explicitly marked historical. |
| [`docs/Archive/phase5_roadmap.md`](Archive/phase5_roadmap.md) | Phase 5 | UX + Graduated Determinism roadmap. Priority tiers P0/P1/P2, feature specs. |
| [`docs/Archive/phase5_implementation_plan.md`](Archive/phase5_implementation_plan.md) | Phase 5 | Phase 5 implementation playbook. M5.1–M5.P2-daemon milestones, all marked complete. 6 PRs (#9–#14). |
| [`docs/Archive/phase6_implementation_plan.md`](Archive/phase6_implementation_plan.md) | Phase 6 | Phase 6 implementation playbook. M6.1–M6.7 milestones, ADRs 1–9, all marked merged. 7 PRs (#15–#21). |

### `dual-brain/docs/phase2/` Mirror (Completed)

These are copies of `docs/phase2/` files that lived inside the `dual-brain/` subtree
for deployment convenience. Archived to eliminate duplication.

| File | Summary |
|---|---|
| [`docs/Archive/dual-brain_2026-06-05_plan_summary.md`](Archive/dual-brain_2026-06-05_plan_summary.md) | Mirror of `2026-06-05_plan_summary.md` from `dual-brain/docs/phase2/`. |
| [`docs/Archive/dual-brain_findings.md`](Archive/dual-brain_findings.md) | Mirror of `findings.md` from `dual-brain/docs/phase2/`. |
| [`docs/Archive/dual-brain_m25_production_migration.md`](Archive/dual-brain_m25_production_migration.md) | Mirror of `m25_production_migration.md` from `dual-brain/docs/phase2/`. |
| [`docs/Archive/dual-brain_phase2_roadmap_CLOSED.md`](Archive/dual-brain_phase2_roadmap_CLOSED.md) | Mirror of `phase2_roadmap_CLOSED_2026-06-11.md` from `dual-brain/docs/phase2/`. |

### Analyses & Reference Materials

| File | Summary |
|---|---|
| [`docs/Archive/DevilsAdvocate_BashComponentAnalysis.md`](Archive/DevilsAdvocate_BashComponentAnalysis.md) | Adversarial critique of the V1 shell trigger architecture. 40 KB analysis comparing V1 to theoretical LLM OS standards. Motivated the dual-brain redesign. |
| [`docs/Archive/dual_brain_naming.md`](Archive/dual_brain_naming.md) | Naming study with 100 candidate name pairs for the two brains. Decision artifact. |
| [`docs/Archive/linux_cabp.md`](Archive/linux_cabp.md) | Linux knowledge base and best practices reference for AI agents. Generic reference, not project-specific. |
| [`docs/Archive/linux_complete_guide.md`](Archive/linux_complete_guide.md) | Comprehensive Linux guide from first principles to production. Generic reference, not project-specific. |
| [`docs/Archive/2026-06-16_code_review_fixes.md`](Archive/2026-06-16_code_review_fixes.md) | Snapshot of code review fixes applied on 2026-06-16. Three fixes completed, seven items deferred. |

### Training Pipeline Artifacts

| File | Summary |
|---|---|
| [`docs/Archive/beginner_synthetic_pipeline_may28.md.md`](Archive/beginner_synthetic_pipeline_may28.md.md) | Early synthetic data generation pipeline notes from May 2026. |
| [`docs/Archive/run4_sft_results.md`](Archive/run4_sft_results.md) | Run4 SFT/DPO training results. Adversarial refusal eval failure (75% vs 95% target), decision to trigger run5. |
| [`docs/Archive/pb_run3_model_card.md`](Archive/pb_run3_model_card.md) | Auto-generated HuggingFace model card stub for run3 checkpoint. Boilerplate. |
| [`docs/Archive/pb_run4_model_card.md`](Archive/pb_run4_model_card.md) | Auto-generated HuggingFace model card stub for run4 checkpoint. Boilerplate. |
| [`docs/Archive/pb_sft_checkpoint500_card.md`](Archive/pb_sft_checkpoint500_card.md) | Auto-generated model card for SFT checkpoint at step 500. Boilerplate. |
| [`docs/Archive/pb_sft_run3_final_card.md`](Archive/pb_sft_run3_final_card.md) | Auto-generated model card for run3 final adapter. Boilerplate. |

### Legacy Claude Memory

Superseded by the `.claude/projects/` memory system. Preserved for reference.

| File | Summary |
|---|---|
| [`docs/Archive/claude-memory-legacy/MEMORY.md`](Archive/claude-memory-legacy/MEMORY.md) | Legacy memory index file. |
| [`docs/Archive/claude-memory-legacy/feedback_no_coauthor.md`](Archive/claude-memory-legacy/feedback_no_coauthor.md) | User preference: no Co-Authored-By lines in commits. |
| [`docs/Archive/claude-memory-legacy/feedback_user_style.md`](Archive/claude-memory-legacy/feedback_user_style.md) | User preference: research thoroughly before any data/model/code change. |
| [`docs/Archive/claude-memory-legacy/phase2-e2e-vm-setup.md`](Archive/claude-memory-legacy/phase2-e2e-vm-setup.md) | Historical notes on Phase 2 end-to-end VM setup (QB→PB→mcpd on GCP T4). |
| [`docs/Archive/claude-memory-legacy/phase4-pb-final.md`](Archive/claude-memory-legacy/phase4-pb-final.md) | Phase 4 close-out: run7_cot_q4km.gguf finalized, run8 rejected. |
| [`docs/Archive/claude-memory-legacy/project_gcp_vm.md`](Archive/claude-memory-legacy/project_gcp_vm.md) | GCP VM inventory: icebreaker-phase2-vm (T4, STOPPED), pb-train-l4 (DELETED). |
| [`docs/Archive/claude-memory-legacy/reference_gemini_schema_transform.md`](Archive/claude-memory-legacy/reference_gemini_schema_transform.md) | Gemini API schema stripping rules ($schema, $id, title, additionalProperties, pattern). |
