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

.PHONY: all-arches base-% iso-% qemu-% clean-out help

help:
	@echo "Icebreaker multi-arch build targets:"
	@echo "  make all-arches LABEL=v6.6 VN=6    # build all arches"
	@echo "  make iso-<arch> LABEL=v6.6 VN=6    # build one arch (amd64|arm64)"
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
	sudo env ARCH=$* bash incremental/build/build-base.sh

# ISO targets depend on base cache existing (the script will build the
# cache if missing, so `make iso-arm64` alone is sufficient — the
# base-% dependency is included for parallelism and clarity).
iso-amd64 iso-arm64: iso-%: base-%
	@echo "── build-iso.sh $(VN) --arch $* --label $(LABEL) ──"
	sudo env ARCH=$* bash incremental/build/build-iso.sh $(VN) --arch $* --label $(LABEL)

# QEMU gate. Uses the arch-suffixed ISO name from build-iso.sh.
qemu-amd64 qemu-arm64: qemu-%:
	@echo "── qemu-gate.sh --arch $* ──"
	env ARCH=$* bash incremental/tests/qemu-gate.sh \
	  incremental/.build/out/$(LABEL)-$*.iso $(VN) $(LABEL)

# Remove built ISOs but keep the base tar caches (avoid re-running the
# 90-minute debootstrap).
clean-out:
	sudo rm -f incremental/.build/out/*.iso incremental/.build/out/*.sha256
