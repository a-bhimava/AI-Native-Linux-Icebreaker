# Memory Index

- [Phase 4 PB Final](phase4-pb-final.md) — Phases 0–4 COMPLETE on main; run7_cot_q4km is the final PB (100% refusal); run8 retrain REJECTED (safety regression)
- [No Claude attribution](feedback_no_coauthor.md) — No Co-Authored-By in commits AND no "Generated with Claude Code" footer in PR bodies
- [User Collaboration Style](feedback_user_style.md) — Thorough research before any data/model/code change; hasty changes have cost the user days
- [Gemini Schema Transform](reference_gemini_schema_transform.md) — Gemini response_schema rejects $schema/$id/title/additionalProperties/pattern; Controller strips them; only a LIVE call catches it
- [Phase 2 E2E VM Setup](phase2-e2e-vm-setup.md) — how QB(Gemini)→PB(local)→mcpd runs end-to-end; PB server bring-up + controller.toml config
- [GCP VM Details](project_gcp_vm.md) — GPU quota=1; icebreaker-phase2-vm (T4) STOPPED w/ run7 deployed; pb-train-l4 DELETED; old VMs retired
