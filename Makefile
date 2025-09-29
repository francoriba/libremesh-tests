
curdir:=tests

OPENWRT_CI_TESTS = \
	$(curdir)/x86-64 \
	$(curdir)/armsr-armv8 \
	$(curdir)/malta-be \
	$(curdir)/shell

test: $(OPENWRT_CI_TESTS)

TESTSDIR ?= $(shell readlink -f $(TOPDIR)/tests)

define pytest
	KEEP_DUT_ON=$(KEEP_DUT_ON) \
	RESET_ALL_DUTS=$(RESET_ALL_DUTS) \
	SUITE_OPTIMIZATION=$(SUITE_OPTIMIZATION) \
	uv --project $(TESTSDIR) run \
		pytest $(TESTSDIR)/tests/ \
		--lg-log \
		--log-cli-level=CONSOLE \
		--lg-colored-steps $(if $(K),-k $(K),)
endef

$(curdir)/setup:
	@[ -n "$$(command -v uv)" ] || \
		(echo "Please install uv. See https://docs.astral.sh/uv/" && exit 1)
	@[ -n "$$(command -v qemu-system-mips)" ] || \
		(echo "Please install qemu-system-mips" && exit 1)
	@[ -n "$$(command -v qemu-system-x86_64)" ] || \
		(echo "Please install qemu-system-x86_64" && exit 1)
	@[ -n "$$(command -v qemu-system-aarch64)" ] || \
		(echo "Please install qemu-system-aarch64" && exit 1)
	@uv --project $(TESTSDIR) sync


$(curdir)/x86-64: QEMU_BIN ?= qemu-system-x86_64
$(curdir)/x86-64: FIRMWARE ?= $(TOPDIR)/bin/targets/x86/64/openwrt-x86-64-generic-squashfs-combined.img.gz
$(curdir)/x86-64:

	[ -f $(FIRMWARE) ]

	gzip \
		--force \
		--keep \
		--decompress \
		$(FIRMWARE) || true

	LG_QEMU_BIN=$(QEMU_BIN) \
		$(pytest) \
		--lg-env $(TESTSDIR)/targets/qemu-x86-64.yaml \
		--firmware $(FIRMWARE:.gz=)

$(curdir)/x86-64-libremesh: QEMU_BIN ?= qemu-system-x86_64
$(curdir)/x86-64-libremesh: FIRMWARE ?= $(TOPDIR)/bin/targets/x86/64/openwrt-x86-64-generic-squashfs-combined.img.gz
$(curdir)/x86-64-libremesh:

	[ -f $(FIRMWARE) ]

	gzip \
		--force \
		--keep \
		--decompress \
		$(FIRMWARE) || true

	LG_QEMU_BIN=$(QEMU_BIN) \
		$(pytest) \
		--lg-env $(TESTSDIR)/targets/qemu-libremesh-x86-64.yaml \
		--firmware $(FIRMWARE:.gz=)

$(curdir)/armsr-armv8: QEMU_BIN ?= qemu-system-aarch64
$(curdir)/armsr-armv8: FIRMWARE ?= $(TOPDIR)/bin/targets/armsr/armv8/openwrt-armsr-armv8-generic-initramfs-kernel.bin
$(curdir)/armsr-armv8:
	[ -f $(FIRMWARE) ]

	LG_QEMU_BIN=$(QEMU_BIN) \
		$(pytest) \
		--lg-env $(TESTSDIR)/targets/qemu-armsr-armv8.yaml \
		--firmware $(FIRMWARE)

$(curdir)/malta-be: QEMU_BIN ?= qemu-system-mips
$(curdir)/malta-be: FIRMWARE ?= $(TOPDIR)/bin/targets/malta/be/openwrt-malta-be-vmlinux-initramfs.elf
$(curdir)/malta-be:
	[ -f $(FIRMWARE) ]

	LG_QEMU_BIN=$(QEMU_BIN) \
		$(pytest) \
		--lg-env $(TESTSDIR)/targets/qemu-malta-be.yaml \
		--firmware $(FIRMWARE)

$(curdir)/gl-mt300n-v2:
	@echo "Running tests on physical GL-MT300N-V2 device..."
	@echo "Make sure the device is connected via serial and Arduino relay"
	$(pytest) \
		--lg-env $(TESTSDIR)/targets/gl-mt300n-v2.yaml \
		--lg-log \
		--log-cli-level=DEBUG

$(curdir)/belkin_rt3200_1:
	@echo "Running tests on physical Belkin RT3200 (1) device..."
	@echo "Make sure the device is connected via serial and Arduino relay (channel 2)"
	$(pytest) \
		--lg-env $(TESTSDIR)/targets/belkin_rt3200_1.yaml \
		--lg-log \
		--log-cli-level=DEBUG

$(curdir)/belkin_rt3200_2:
	@echo "Running tests on physical Belkin RT3200 (2) device..."
	@echo "Make sure the device is connected via serial and Arduino relay (channel 3)"
	$(pytest) \
		--lg-env $(TESTSDIR)/targets/belkin_rt3200_2.yaml \
		--lg-log \
		--log-cli-level=DEBUG

$(curdir)/mesh_belkin_pair:
	@echo "Running mesh tests on Belkin RT3200 pair..."
	@echo "Make sure both devices are connected via serial and Arduino relay"
	KEEP_DUT_ON=$(KEEP_DUT_ON) \
	RESET_ALL_DUTS=$(RESET_ALL_DUTS) \
	SUITE_OPTIMIZATION=$(SUITE_OPTIMIZATION) \
	$(pytest) \
		--lg-env $(TESTSDIR)/targets/mesh_belkin_pair.yaml \
		--lg-log \
		--log-cli-level=DEBUG
