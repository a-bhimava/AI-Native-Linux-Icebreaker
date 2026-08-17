# Makefile — Icebreaker multi-arch build orchestration.
#
# Operator commands:
#   make all-arches LABEL=v6.6 VN=6        # build both amd64 + arm64 sequentially
#   make iso-amd64 LABEL=v6.6 VN=6         # build only amd64
#   make iso-arm64 LABEL=v6.6 VN=6         # build only arm64
#   make qemu-arm64 LABEL=v6.6 VN=6        # QEMU-gate a built ISO
#   make base-arm64                        # rebuild the arm64 base cache
#
# Backwards-compat: `sudo bash incremental/build/build-iso.sh 6 --label v6.51`
# without --arch still produces amd64 (build-iso.sh defaults ARCH=amd64).
#
# Sequential in this cut because the GCP build VM has 8 GB RAM and arm64
# debootstrap under qemu-user-static is memory-hungry. V6.7+: upgrade to
# 32 GB VM and add `&` for parallel builds.

LABEL  ?= v6.6
VN     ?= 6
ARCHES ?= amd64 arm64
PROFILE ?= desktop

.PHONY: all-arches base-% iso-% qemu-% clean-out help

help:
	@echo "Icebreaker multi-arch build targets:"
	@echo "  make all-arches LABEL=v6.6 VN=6 PROFILE=desktop    # build all arches"
	@echo "  make iso-<arch> LABEL=v6.6 VN=6 PROFILE=xfce-frosted # build one arch"
	@echo "  make base-<arch>                   # rebuild base cache for arch"
	@echo "  make qemu-<arch> LABEL=v6.6 VN=6   # QEMU-gate a built ISO"
	@echo "  make clean-out                     # remove built ISOs (keeps caches)"

# Build one ISO per arch.
all-arches: $(addprefix iso-,$(ARCHES))

# Base cache targets. Sequential (no & separator) because arm64
# debootstrap eats RAM. First arm64 base takes ~90 min under
# qemu-user-static; cached afterward.
base-amd64 base-arm64: base-%:
	@echo "── build-base.sh --arch $* ──"
	sudo env ARCH=$* PROFILE=$(PROFILE) bash incremental/build/build-base.sh --profile $(PROFILE)

# ISO targets depend on base cache existing (the script will build the
# cache if missing, so `make iso-arm64` alone is sufficient — the
# base-% dependency is included for parallelism and clarity).
# v6.13_OC Fix L' Commit 1: forward EDITION={current,oc} to build-iso.sh
# so the OC-edition build ships opencode + qb_oc.json instead of the
# Textual TUI. Default `current` preserves the v6.12 shape.
EDITION ?= current
iso-amd64 iso-arm64: iso-%: base-%
	@echo "── build-iso.sh $(VN) --profile $(PROFILE) --arch $* --label $(LABEL) --edition $(EDITION) ──"
	sudo env ARCH=$* PROFILE=$(PROFILE) EDITION=$(EDITION) bash incremental/build/build-iso.sh $(VN) --profile $(PROFILE) --arch $* --label $(LABEL) --edition $(EDITION)

# QEMU gate. Uses the arch-suffixed ISO name from build-iso.sh.
qemu-amd64 qemu-arm64: qemu-%:
	@echo "── qemu-gate.sh --arch $* ──"
	env ARCH=$* PROFILE=$(PROFILE) EDITION=$(EDITION) bash incremental/tests/qemu-gate.sh \
	  incremental/.build/out/$(LABEL)$(if $(filter oc,$(EDITION)),_OC)-$*.iso $(VN) $(LABEL)

# Remove built ISOs but keep the base tar caches (avoid re-running the
# 90-minute debootstrap).
clean-out:
	sudo rm -f incremental/.build/out/*.iso incremental/.build/out/*.sha256
