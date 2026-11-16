# Builds the DOS programs with NASM (8086 code, .COM files).
NASM = nasm
NASMFLAGS = -f bin -I src/

# Windows drivers: OpenWatcom v2 (only wlink is used). WATCOM = where the
# snapshot is unpacked; OWBIN = its host binary folder: armo64 (macOS arm64),
# bino64 (macOS x64), binl64 (Linux x64), binnt64 (Windows x64).
WATCOM ?= $(HOME)/ow
OWBIN ?= armo64
WLINK = WATCOM=$(WATCOM) $(WATCOM)/$(OWBIN)/wlink
INC = src/ps2lpt.inc src/ps2init.inc src/ps2data.inc src/bits.inc src/burst.inc

all: lptmouse.com lpmtest.com

windows: build/lptmouse.drv build/lptmouse.386 build/lptmouse.vxd build/lptmouse.inf

build/lptmdrv.obj: src/lptmdrv.asm $(INC) src/dosplat.inc
	mkdir -p build
	$(NASM) -f obj -I src/ -o $@ src/lptmdrv.asm

build/lptmvxd.obj: src/lptmvxd.asm $(INC) src/vxdplat.inc
	mkdir -p build
	$(NASM) -f obj -I src/ -o $@ src/lptmvxd.asm

build/lptmouse.drv: build/lptmdrv.obj src/lptmdrv.lnk
	$(WLINK) @src/lptmdrv.lnk

build/lptmouse.386: build/lptmvxd.obj src/lptmvxd.lnk
	$(WLINK) @src/lptmvxd.lnk

build/lptm95.obj: src/lptm95.asm $(INC) src/vxdplat.inc
	mkdir -p build
	$(NASM) -f obj -I src/ -o $@ src/lptm95.asm

build/lptmouse.vxd: build/lptm95.obj src/lptm95.lnk
	$(WLINK) @src/lptm95.lnk

build/lptmouse.inf: src/lptmouse.inf
	mkdir -p build
	sed 's/$$/\r/' src/lptmouse.inf > $@

lptmouse.com: src/lptmouse.asm src/ps2lpt.inc src/ps2init.inc src/util.inc src/dosplat.inc src/burst.inc src/ps2data.inc src/bits.inc
	$(NASM) $(NASMFLAGS) -l lptmouse.lst -o $@ src/lptmouse.asm

lpmtest.com: src/lpmtest.asm src/ps2lpt.inc src/ps2init.inc src/util.inc src/dosplat.inc src/burst.inc src/ps2data.inc src/bits.inc
	$(NASM) $(NASMFLAGS) -o $@ src/lpmtest.asm

clean:
	rm -f lptmouse.com lpmtest.com lptmouse.lst
	rm -rf build

.PHONY: all windows clean
