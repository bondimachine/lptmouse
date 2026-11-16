"""Tests for the Windows drivers without Windows.

LPTMOUSE.DRV (16-bit NE DLL) is loaded by a minimal NE loader into the real
mode machine of sim.py (selectors are plain paragraphs there), with the
KERNEL imports replaced by stubs. It is tested in standard mode (it reads
the simulated mouse itself) and in enhanced mode (with a fake LPTMOUSE.386
API).

LPTMOUSE.386 (LE VxD) is loaded by a minimal LE loader into a 32-bit flat
machine with fake VMM / VPICD services and the same simulated mouse.

Usage: python winsim.py   (after `make windows`)
"""
import os
import struct
import sys

from unicorn import (Uc, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE, UC_HOOK_INTR,
                     UC_HOOK_INSN)
from unicorn.x86_const import *

import sim
from sim import Sim, PS2Mouse, check, FAILS, ROOT, BIOS_SEG, LPT

BUILD = os.path.join(ROOT, 'build')


def s8(v):
    return v - 256 if v & 0x80 else v


def s16(v):
    return v - 0x10000 if v & 0x8000 else v


# ===========================================================================
# NE loader + Windows driver machine
# ===========================================================================
STUB_GPPS = 0x3000      # GetPrivateProfileString: int F1; retf 22
STUB_GPPI = 0x3010      # GetPrivateProfileInt:    int F2; retf 14
STUB_EVENT = 0x3020     # event_proc:              int F8; retf
STUB_VXD = 0x3030       # fake LPTMOUSE.386 API:   int F9; retf


class WinSim(Sim):
    def __init__(self, enhanced=False, ini=None, **kw):
        super().__init__(**kw)
        self.enhanced = enhanced
        self.ini = ini or {}
        self.events = []
        self.vxd_calls = []
        self.vxd_running = False
        self.vxd_queue = []
        self.lpt_access = 0
        b = BIOS_SEG * 16
        self.mu.mem_write(b + STUB_GPPS, bytes([0xCD, 0xF1, 0xCA, 22, 0]))
        self.mu.mem_write(b + STUB_GPPI, bytes([0xCD, 0xF2, 0xCA, 14, 0]))
        self.mu.mem_write(b + STUB_EVENT, bytes([0xCD, 0xF8, 0xCB]))
        self.mu.mem_write(b + STUB_VXD, bytes([0xCD, 0xF9, 0xCB]))

    # (Sim registered self._in / self._out as hooks, so these overrides apply)
    def _in(self, uc, port, size, ud):
        if LPT <= port <= LPT + 2:
            self.lpt_access += 1
        return super()._in(uc, port, size, ud)

    def _out(self, uc, port, size, value, ud):
        if LPT <= port <= LPT + 2:
            self.lpt_access += 1
        return super()._out(uc, port, size, value, ud)

    def cstr(self, seg, off):
        a = seg * 16 + off
        out = b''
        while True:
            c = self.r8(a)
            if c == 0:
                return out.decode()
            out += bytes([c])
            a += 1

    def _intr(self, uc, intno, ud):
        r = uc.reg_read
        ss, sp = r(UC_X86_REG_SS), r(UC_X86_REG_SP)
        arg = lambda n: self.r16(ss * 16 + sp + 4 + n)       # after the far call
        if intno == 0xF1:                                     # GetPrivateProfileString
            app = self.cstr(arg(20), arg(18))
            key = self.cstr(arg(16), arg(14))
            dflt = self.cstr(arg(12), arg(10))
            size = arg(4)
            val = self.ini.get((app.lower(), key.lower()), dflt)[:size - 1]
            self.mu.mem_write(arg(8) * 16 + arg(6), val.encode() + b'\0')
            uc.reg_write(UC_X86_REG_AX, len(val))
        elif intno == 0xF2:                                   # GetPrivateProfileInt
            app = self.cstr(arg(12), arg(10))
            key = self.cstr(arg(8), arg(6))
            dflt = arg(4)
            v = self.ini.get((app.lower(), key.lower()))
            uc.reg_write(UC_X86_REG_AX, int(v) if v is not None else dflt)
        elif intno == 0xF8:                                   # event_proc
            self.events.append(dict(ax=r(UC_X86_REG_AX), bx=s16(r(UC_X86_REG_BX)),
                                    cx=s16(r(UC_X86_REG_CX)), dx=r(UC_X86_REG_DX),
                                    if_=bool(r(UC_X86_REG_EFLAGS) & 0x200)))
        elif intno == 0xF9:                                   # fake VxD API
            self._fake_vxd(uc)
        elif intno == 0x2F and self.enhanced:
            ax = r(UC_X86_REG_AX)
            if ax == 0x1600:
                uc.reg_write(UC_X86_REG_AX, 0x0A03)
            elif ax == 0x1684 and r(UC_X86_REG_BX) == 0x3A7D:
                uc.reg_write(UC_X86_REG_ES, BIOS_SEG)
                uc.reg_write(UC_X86_REG_DI, STUB_VXD)
        elif intno == 0x2F:
            pass                                              # no Windows services
        else:
            super()._intr(uc, intno, ud)

    def _fake_vxd(self, uc):
        r = uc.reg_read
        ax = r(UC_X86_REG_AX)
        self.vxd_calls.append((ax, r(UC_X86_REG_BX), r(UC_X86_REG_CX), r(UC_X86_REG_DX)))
        cf = False
        if ax == 1:
            self.vxd_running = True
            uc.reg_write(UC_X86_REG_AX, 0)
        elif ax == 3:
            self.vxd_running = False
        elif ax == 4:
            self.vxd_running = True
        elif ax == 2:
            if self.vxd_queue:
                b0, b1, b2 = self.vxd_queue.pop(0)
                uc.reg_write(UC_X86_REG_BX, b0)
                uc.reg_write(UC_X86_REG_CX, b1)
                uc.reg_write(UC_X86_REG_DX, b2)
                uc.reg_write(UC_X86_REG_AX, len(self.vxd_queue))
            else:
                cf = True
        self._set_cf(cf)

    def vxd_packet(self, b0, b1, b2):
        """The fake VxD got a packet: queue it and raise the virtual IRQ."""
        self.vxd_queue.append((b0, b1, b2))
        if self.vxd_running:
            self.irr |= 0x80
            self.irq7_raised_at = self.t

    # --- NE loading ---
    def load_ne(self, path, code_seg=0x2000, data_seg=0x2800):
        d = open(path, 'rb').read()
        ne = struct.unpack_from('<I', d, 0x3C)[0]
        assert d[ne:ne + 2] == b'NE'
        (ent_off, ent_len) = struct.unpack_from('<HH', d, ne + 4)
        ip, cs = struct.unpack_from('<HH', d, ne + 0x14)
        nseg, nmod = struct.unpack_from('<HH', d, ne + 0x1C)
        seg_tab, _, resn, modref, impn = struct.unpack_from('<HHHHH', d, ne + 0x22)
        shift = struct.unpack_from('<H', d, ne + 0x32)[0]
        bases = {1: code_seg, 2: data_seg}
        modnames = []
        for i in range(nmod):
            off = struct.unpack_from('<H', d, ne + modref + 2 * i)[0]
            ln = d[ne + impn + off]
            modnames.append(d[ne + impn + off + 1:ne + impn + off + 1 + ln].decode())
        self.modname = d[ne + resn + 1:ne + resn + 1 + d[ne + resn]].decode()
        imports = {('KERNEL', 'GETPRIVATEPROFILESTRING'): (BIOS_SEG, STUB_GPPS),
                   ('KERNEL', 'GETPRIVATEPROFILEINT'): (BIOS_SEG, STUB_GPPI),
                   ('KERNEL', 193): (0x0040, 0)}
        for i in range(nseg):
            sec, length, flags, alloc = struct.unpack_from('<HHHH', d, ne + seg_tab + 8 * i)
            base = bases[i + 1] * 16
            data = d[sec << shift:(sec << shift) + length]
            self.mu.mem_write(base, data)
            if flags & 0x100:                                 # relocations
                rp = (sec << shift) + length
                count = struct.unpack_from('<H', d, rp)[0]
                for k in range(count):
                    stype, rflags, soff, t1, t2 = struct.unpack_from('<BBHHH', d, rp + 2 + 8 * k)
                    kind = rflags & 3
                    if kind == 0:                             # internal
                        seg_no = t1 & 0xFF
                        tseg, toff = bases[seg_no], t2
                    elif kind == 1:                           # import by ordinal
                        tseg, toff = imports[(modnames[t1 - 1], t2)]
                    elif kind == 2:                           # import by name
                        ln = d[ne + impn + t2]
                        name = d[ne + impn + t2 + 1:ne + impn + t2 + 1 + ln].decode().upper()
                        tseg, toff = imports[(modnames[t1 - 1], name)]
                    else:
                        raise RuntimeError('OS fixup')
                    value = {2: tseg, 5: toff, 3: None}[stype] if stype in (2, 5, 3) else None
                    if stype == 5 and kind == 1 and t2 == 193:
                        value = tseg                          # constant import
                    additive = rflags & 4
                    off = soff
                    while True:                               # follow the chain
                        nxt = self.r16(base + off)
                        if stype == 3:
                            self.w16(base + off, toff)
                            self.w16(base + off + 2, tseg)
                        elif additive:
                            self.w16(base + off, nxt + value)
                        else:
                            self.w16(base + off, value)
                        if additive or nxt == 0xFFFF or stype == 3:
                            break
                        off = nxt
        # entry table: fixed bundles
        self.entries = {}
        p, ordinal = ne + ent_off, 1
        while True:
            cnt, segi = d[p], d[p + 1]
            p += 2
            if cnt == 0:
                break
            for _ in range(cnt):
                if segi == 0:
                    pass
                elif segi == 0xFF:
                    p += 6
                else:
                    off = struct.unpack_from('<H', d, p + 1)[0]
                    self.entries[ordinal] = (bases[segi], off)
                    p += 3
                ordinal += 1
            if segi == 0:
                pass
        self.entry_point = (bases[cs], ip)
        self.data_seg = data_seg

    def far_call(self, target, args=(), ds=None):
        """Pascal far call: args pushed in order (each a 16-bit word)."""
        mu = self.mu
        mu.reg_write(UC_X86_REG_SS, 0x9000)
        mu.reg_write(UC_X86_REG_SP, 0xFFF0)
        for a in args:
            self._push(a)
        self._push(BIOS_SEG)
        self._push(sim.RET_IP)
        mu.reg_write(UC_X86_REG_DS, ds if ds is not None else 0x9000)
        mu.reg_write(UC_X86_REG_ES, 0)
        mu.reg_write(UC_X86_REG_CS, target[0])
        mu.reg_write(UC_X86_REG_IP, target[1])
        mu.reg_write(UC_X86_REG_EFLAGS, 0x202)
        self.run(until_lin=BIOS_SEG * 16 + sim.RET_IP, until_t=self.t + 5e6)
        return mu.reg_read(UC_X86_REG_AX)


def drv_common(ws, label):
    ws.load_ne(os.path.join(BUILD, 'lptmouse.drv'))
    check(ws.modname == 'MOUSE', '%s: module name MOUSE' % label)
    check(all(o in ws.entries for o in (1, 2, 3, 4)), '%s: exports 1-4' % label)
    ax = ws.far_call(ws.entry_point, ds=ws.data_seg)
    check(ax == 1, '%s: LibEntry returns 1' % label)
    ax = ws.far_call(ws.entries[1], (0x9000, 0x0100))           # Inquire
    info = bytes(ws.mu.mem_read(0x90100, 14))
    check(ax == 14 and info[0] == 0xFF and info[1] == 0xFF and info[2] == 2,
          '%s: Inquire -> exists, 2 buttons (%s)' % (label, info.hex()))
    vec = s16(ws.far_call(ws.entries[4]))
    check(vec == 0x0F, '%s: MouseGetIntVect = 0Fh (%d)' % (label, vec))
    ws.far_call(ws.entries[2], (BIOS_SEG, STUB_EVENT))            # Enable
    check(ws.r16(0x0F * 4 + 2) == 0x2000, '%s: IRQ7 vector hooked' % label)
    check(not (ws.imr & 0x80), '%s: IRQ7 unmasked' % label)


def summarize(events):
    dx = sum(e['bx'] for e in events if e['ax'] & 1)
    dy = sum(e['cx'] for e in events if e['ax'] & 1)
    flags = [e['ax'] & 0x7E for e in events if e['ax'] & 0x7E]
    return dx, dy, flags


def drv_standard_test(latency=0.0):
    label = 'DRV standard%s' % (' +%dus' % latency if latency else '')
    print('== ' + label)
    ws = WinSim(irq_latency_us=latency)
    drv_common(ws, label)
    t0 = ws.t + 5000
    moves = [(4, -2, 0)] * 5 + [(0, 0, 1), (0, 0, 0), (0, 0, 2), (0, 0, 0),
                                (0, 0, 4), (0, 0, 0), (-100, 50, 0)]
    for i, (dx, dy, b) in enumerate(moves):
        ws.move(t0 + i * 25000, dx, dy, b)
    ws.idle(len(moves) * 25000 + 200000)
    dx, dy, flags = summarize(ws.events)
    check((dx, dy) == (20 - 100, 10 - 50), '%s: movement %d,%d (want -80,-40)' % (label, dx, dy))
    check(flags == [0x02, 0x04, 0x08, 0x10, 0x20, 0x40],
          '%s: buttons L/R/M down-up: %s' % (label, [hex(f) for f in flags]))
    check(all(e['dx'] == 2 for e in ws.events), '%s: DX = 2 buttons' % label)
    check(all(e['if_'] for e in ws.events), '%s: event_proc called with IF=1' % label)
    ws.far_call(ws.entries[3])                                    # Disable
    check(ws.r16(0x0F * 4 + 2) == BIOS_SEG, '%s: vector restored' % label)
    check(ws.imr & 0x80, '%s: IRQ7 masked again' % label)
    check(ws.lpt_ctrl & 0x10 == 0, '%s: port IRQ off after Disable' % label)


def drv_enhanced_test():
    label = 'DRV enhanced (fake VxD)'
    print('== ' + label)
    ws = WinSim(enhanced=True, ini={('lptmouse', 'port'): '278', ('lptmouse', 'irq'): '7',
                                    ('lptmouse', 'rate'): '100', ('lptmouse', 'invert'): '1'})
    drv_common(ws, label)
    start = [c for c in ws.vxd_calls if c[0] == 1]
    check(start == [(1, 0x278, 0x0307, 100)],
          '%s: VxD start(port 278, IRQ 7, invert, rate 100): %s' % (label, start))
    check([c[0] for c in ws.vxd_calls[:3]] == [1, 3, 4],
          '%s: start, stop until Enable, resume' % label)
    ws.lpt_access = 0
    for i in range(3):
        ws.vxd_packet(0x28, 5, 0xFD)                              # right 5, down 3
    ws.vxd_packet(0x09, 0, 0)
    ws.vxd_packet(0x08, 0, 0)
    ws.idle(20000)
    dx, dy, flags = summarize(ws.events)
    check((dx, dy) == (15, 9) and flags == [0x02, 0x04],
          '%s: events from the VxD: %d,%d %s' % (label, dx, dy, [hex(f) for f in flags]))
    ws.far_call(ws.entries[3])
    check(ws.vxd_calls[-1][0] == 3, '%s: Disable stops the VxD' % label)
    check(ws.lpt_access == 0, '%s: driver never touches the LPT ports' % label)


# ===========================================================================
# LE loader + VxD machine
# ===========================================================================
LE_BASE = 0x00100000
STOP = 0x00090000                 # return address: hlt
CLIENT = 0x00080000
VM_HANDLE = 0x00C01000
IRQ_HANDLE = 0x00C02000
DEVNODE = 0x00C03000
INSTANCE = 0x00060000


class VxdSim:
    def __init__(self, insn_us=0.05, io_us=1.0, latency=0.0, lpt=LPT):
        self.lpt = lpt
        self.mu = Uc(UC_ARCH_X86, UC_MODE_32)
        self.mu.mem_map(0, 0x01000000)
        self.mu.mem_write(STOP, b'\xF4')
        self.t = 0.0
        self.insn_us, self.io_us, self.latency = insn_us, io_us, latency
        self.lpt_data, self.lpt_ctrl = 0xFF, 0x0C
        self.host_clk_low = self.host_data_low = False
        self.prev_clk = True
        self.mouse = PS2Mouse(self)
        self.hw_pending_at = None
        self.virt_req = False
        self.phys_eois = 0
        self.virtualized = None
        self.services = []
        self.packets = []
        self.posted = []
        self.registry = {}
        self.unmasked = False
        self.mouse_data = None
        self.until_stop = False
        mu = self.mu
        mu.hook_add(UC_HOOK_CODE, self._code)
        mu.hook_add(UC_HOOK_INTR, self._intr)
        mu.hook_add(UC_HOOK_INSN, self._in, None, 1, 0, UC_X86_INS_IN)
        mu.hook_add(UC_HOOK_INSN, self._out, None, 1, 0, UC_X86_INS_OUT)

    def r32(self, a): return struct.unpack('<I', self.mu.mem_read(a, 4))[0]
    def w32(self, a, v): self.mu.mem_write(a, struct.pack('<I', v & 0xFFFFFFFF))

    def load_le(self, path):
        d = open(path, 'rb').read()
        le = struct.unpack_from('<I', d, 0x3C)[0]
        assert d[le:le + 2] == b'LE'
        h = lambda o: struct.unpack_from('<I', d, le + o)[0]
        self.os_type = struct.unpack_from('<H', d, le + 0x0A)[0]
        self.module_flags = h(0x10)
        npages, page_size = h(0x14), h(0x28)
        obj_tab, nobj = h(0x40), h(0x44)
        ent_tab = h(0x5C)
        fpt, frt = h(0x68), h(0x6C)
        data_pages = h(0x80)
        assert nobj == 1
        vsize, _, oflags, pidx, pcnt, _ = struct.unpack_from('<6I', d, le + obj_tab)
        img = d[data_pages:data_pages + npages * page_size]
        self.mu.mem_write(LE_BASE, img[:vsize])
        for page in range(npages):
            a, b = struct.unpack_from('<II', d, le + fpt + 4 * page)
            p = le + frt + a
            while p < le + frt + b:
                stype, rflags = d[p], d[p + 1]
                assert stype == 7 and rflags & 3 == 0, 'unexpected fixup %02X %02X' % (stype, rflags)
                soff = struct.unpack_from('<h', d, p + 2)[0]
                p += 4
                obj = d[p]
                p += 1
                if rflags & 0x10:
                    toff = struct.unpack_from('<I', d, p)[0]
                    p += 4
                else:
                    toff = struct.unpack_from('<H', d, p)[0]
                    p += 2
                assert obj == 1
                self.w32(LE_BASE + page * page_size + soff, LE_BASE + toff)
        cnt, typ = d[le + ent_tab], d[le + ent_tab + 1]
        assert cnt >= 1 and typ == 3
        off = struct.unpack_from('<I', d, le + ent_tab + 4 + 1)[0]
        self.ddb = LE_BASE + off
        self.control_proc = self.r32(self.ddb + 0x18)
        self.pm_api = self.r32(self.ddb + 0x20)
        self.ddb_name = bytes(self.mu.mem_read(self.ddb + 0x0C, 8)).decode()
        self.ddb_id = struct.unpack('<H', self.mu.mem_read(self.ddb + 6, 2))[0]

    # lines / port
    def check_edge(self):
        clk = self.mouse.clk()
        if clk and not self.prev_clk and self.lpt_ctrl & 0x10 and self.virtualized:
            if self.hw_pending_at is None:
                self.hw_pending_at = self.t
        self.prev_clk = clk

    def _code(self, uc, addr, size, ud):
        self.t += self.insn_us
        if self.mouse.wake <= self.t:
            self.mouse.update(self.t)

    def _in(self, uc, port, size, ud):
        self.t += self.io_us
        self.mouse.update(self.t)
        if port == self.lpt:
            return self.lpt_data
        if port == self.lpt + 1:
            return 0x08 | (0x40 if self.mouse.clk() else 0) | (0x10 if self.mouse.data() else 0)
        if port == self.lpt + 2:
            return self.lpt_ctrl
        return 0xFF

    def _out(self, uc, port, size, value, ud):
        self.t += self.io_us
        self.mouse.update(self.t)
        if port == self.lpt:
            self.lpt_data = value & 0xFF
            self.host_clk_low = (self.lpt_data & 1) == 0
            self.host_data_low = (self.lpt_data & 2) == 0
            self.check_edge()
        elif port == self.lpt + 2:
            self.lpt_ctrl = value & 0xFF

    def _intr(self, uc, intno, ud):
        assert intno == 0x20, 'INT %02X' % intno
        eip = uc.reg_read(UC_X86_REG_EIP)
        svc = self.r32(eip)
        uc.reg_write(UC_X86_REG_EIP, eip + 4)
        self.services.append(svc)
        fl = uc.reg_read(UC_X86_REG_EFLAGS) & ~1
        if svc == 0x00010003:                                     # Get_Sys_VM_Handle
            uc.reg_write(UC_X86_REG_EBX, VM_HANDLE)
        elif svc == 0x0001003F:                                   # Get_System_Time
            uc.reg_write(UC_X86_REG_EAX, int(self.t / 1000))
        elif svc == 0x00030001:                                   # VPICD_Virtualize_IRQ
            edi = uc.reg_read(UC_X86_REG_EDI)
            irq = struct.unpack('<H', uc.mem_read(edi, 2))[0]
            self.virtualized = dict(irq=irq, hw=self.r32(edi + 4), eoi=self.r32(edi + 12))
            uc.reg_write(UC_X86_REG_EAX, IRQ_HANDLE)
        elif svc == 0x00030002:                                   # Set_Int_Request
            assert uc.reg_read(UC_X86_REG_EAX) == IRQ_HANDLE
            assert uc.reg_read(UC_X86_REG_EBX) == VM_HANDLE
            self.virt_req = True
        elif svc == 0x00030003:                                   # Clear_Int_Request
            self.virt_req = False
        elif svc == 0x00030004:                                   # Phys_EOI
            assert uc.reg_read(UC_X86_REG_EAX) == IRQ_HANDLE
            self.phys_eois += 1
        elif svc == 0x00030009:                                   # Physically_Unmask
            assert uc.reg_read(UC_X86_REG_EAX) == IRQ_HANDLE
            self.unmasked = True
        elif svc == 0x00030011:                                   # Force_Default_Behavior
            assert uc.reg_read(UC_X86_REG_EAX) == IRQ_HANDLE
            self.virtualized = None
        elif svc == 0x000C0006:                                   # VMD_Set_Mouse_Data
            ax = uc.reg_read(UC_X86_REG_EAX)
            dx = uc.reg_read(UC_X86_REG_EDX)
            self.mouse_data = dict(type=(ax >> 8) & 0xFF, irq=ax & 0xFF,
                                   buttons=(dx >> 8) & 0xFF, port=dx & 0xFF)
        elif svc == 0x000C0003:                                   # VMD_Post_Pointer_Message
            def sgn(v): return v - (1 << 32) if v & 0x80000000 else v
            self.posted.append((uc.reg_read(UC_X86_REG_EAX) & 0xFF,
                                sgn(uc.reg_read(UC_X86_REG_ESI)),
                                sgn(uc.reg_read(UC_X86_REG_EDI)),
                                bool(uc.reg_read(UC_X86_REG_EFLAGS) & 0x200)))
        elif svc == 0x0033003E:                                   # _CONFIGMG_Read_Registry_Value
            esp = uc.reg_read(UC_X86_REG_ESP)
            a = [self.r32(esp + 4 * i) for i in range(7)]
            name = b''
            p = a[2]
            while self.mu.mem_read(p, 1)[0]:
                name += bytes(self.mu.mem_read(p, 1)); p += 1
            assert a[0] == DEVNODE and a[6] == 1 and a[3] == 3, 'registry call args %s' % a
            v = self.registry.get(name.decode())
            if v is None:
                uc.reg_write(UC_X86_REG_EAX, 0x25)                # CR_NO_SUCH_VALUE
            else:
                self.mu.mem_write(a[4], struct.pack('<I', v))
                self.w32(a[5], 4)
                uc.reg_write(UC_X86_REG_EAX, 0)
        else:
            raise RuntimeError('service %08X' % svc)
        uc.reg_write(UC_X86_REG_EFLAGS, fl)

    def call(self, proc, regs=None, iflag=True):
        mu = self.mu
        mu.reg_write(UC_X86_REG_ESP, 0x00070000)
        for k, v in (regs or {}).items():
            mu.reg_write(k, v)
        esp = mu.reg_read(UC_X86_REG_ESP) - 4
        self.w32(esp, STOP)
        mu.reg_write(UC_X86_REG_ESP, esp)
        mu.reg_write(UC_X86_REG_EFLAGS, 0x202 if iflag else 0x002)
        mu.emu_start(proc, STOP, timeout=0, count=0)
        return mu.reg_read(UC_X86_REG_EFLAGS) & 1

    def api(self, ax, bx=0, cx=0, dx=0):
        c = bytearray(0x48)
        struct.pack_into('<I', c, 0x1C, ax)
        struct.pack_into('<I', c, 0x10, bx)
        struct.pack_into('<I', c, 0x18, cx)
        struct.pack_into('<I', c, 0x14, dx)
        self.mu.mem_write(CLIENT, bytes(c))
        self.call(self.pm_api, {UC_X86_REG_EBP: CLIENT, UC_X86_REG_EBX: VM_HANDLE})
        g = lambda o: self.r32(CLIENT + o)
        return dict(cf=g(0x2C) & 1, ax=g(0x1C) & 0xFFFF, bx=g(0x10) & 0xFFFF,
                    cx=g(0x18) & 0xFFFF, dx=g(0x14) & 0xFFFF, si=g(0x04) & 0xFFFF,
                    di=g(0x00) & 0xFFFF)

    def idle(self, us):
        """Let time pass: deliver hardware IRQs and run the 'System VM'."""
        end = self.t + us
        while self.t < end:
            self.t += 10.0
            self.mouse.update(self.t)
            if self.hw_pending_at is not None and self.t - self.hw_pending_at >= self.latency:
                self.hw_pending_at = None
                self.call(self.virtualized['hw'], {UC_X86_REG_EAX: IRQ_HANDLE,
                                                   UC_X86_REG_EBX: VM_HANDLE}, iflag=False)
            if self.virt_req:                     # System VM interrupt handler
                while True:
                    r = self.api(2)
                    if r['cf']:
                        break
                    self.packets.append((r['bx'] & 0xFF, r['cx'] & 0xFF, r['dx'] & 0xFF))
                self.call(self.virtualized['eoi'], {UC_X86_REG_EAX: IRQ_HANDLE,
                                                    UC_X86_REG_EBX: VM_HANDLE})


def vxd_test(label, insn_us=0.05, io_us=1.0, latency=0.0):
    print('== VxD ' + label)
    v = VxdSim(insn_us, io_us, latency)
    v.load_le(os.path.join(BUILD, 'lptmouse.386'))
    check(v.os_type == 4 and v.module_flags == 0x28000, 'LE: Win386 static VxD')
    check(v.ddb_name == 'LPTMOUSE' and v.ddb_id == 0x3A7D, 'DDB name / id')
    check(v.call(v.control_proc, {UC_X86_REG_EAX: 1, UC_X86_REG_EBX: VM_HANDLE}) == 0,
          'Device_Init ok')
    r = v.api(0)
    check(r['ax'] == 0x0100 and not r['cf'], 'API version 1.0')
    v.lpt_data, v.lpt_ctrl = 0xAA, 0x0C                     # what was there before
    r = v.api(1, bx=0x378, cx=0x0007, dx=40)
    check(not r['cf'] and r['ax'] == 0, 'start: CF=%d AX=%02X' % (r['cf'], r['ax']))
    check(v.virtualized and v.virtualized['irq'] == 7, 'IRQ 7 virtualized')
    rx = [e[2] for e in v.mouse.log if e[1] == 'rx']
    check(rx == [0xFF, 0xF3, 40, 0xF4], 'mouse init sequence %s' % [hex(b) for b in rx])
    v.api(3)
    v.api(4)
    t0 = v.t + 5000
    moves = [(3, -1, 0)] * 6 + [(0, 0, 1), (0, 0, 0), (-50, 20, 0)]
    for i, (dx, dy, b) in enumerate(moves):
        v.mouse.moves.append((t0 + i * 25000, dx, dy, b))
    v.idle(len(moves) * 25000 + 100000)
    want = [(0x08 | b | (0x10 if dx < 0 else 0) | (0x20 if dy < 0 else 0), dx & 0xFF, dy & 0xFF)
            for dx, dy, b in moves]
    check(v.packets == want, 'packets through the virtual IRQ: %d/%d' % (len(v.packets), len(want)))
    check(not v.virt_req, 'virtual request cleared after EOI')
    c = v.api(5)
    print('   counters: packets %d, errors %d, resends %d, dropped %d, reads/ms %d'
          % (c['bx'], c['cx'], c['dx'], c['si'], c['di']))
    check(c['cx'] == 0 and c['si'] == 0, 'no frame errors / drops')
    v.call(v.control_proc, {UC_X86_REG_EAX: 5, UC_X86_REG_EBX: VM_HANDLE})   # System_Exit
    check((v.lpt_data, v.lpt_ctrl & 0x1F) == (0xAA, 0x0C), 'port state restored on exit')


def vxd95_test(label, insn_us=0.05, io_us=1.0, latency=0.0, registry=None):
    print('== Win95 mini-driver ' + label)
    port = (registry or {}).get('Port', 0x378)
    v = VxdSim(insn_us, io_us, latency, lpt=port)
    v.load_le(os.path.join(BUILD, 'lptmouse.vxd'))
    check(v.os_type == 4 and v.module_flags == 0x38000, 'LE: Win386 dynamic VxD')
    check(v.ddb_name == 'LPTM95  ', 'DDB name')
    ddb_size = v.r32(v.ddb + 0x40)
    check(struct.unpack('<H', v.mu.mem_read(v.ddb + 4, 2))[0] == 0x400 and ddb_size == 0x50,
          'DDB 4.0 layout (size %02X)' % ddb_size)
    v.registry = registry if registry is not None else \
        {'Port': 0x378, 'IRQ': 7, 'Rate': 40, 'Invert': 0}
    v.lpt_data, v.lpt_ctrl = 0xAA, 0x0C
    inst = bytearray(0x28)
    struct.pack_into('<I', inst, 0x0C, DEVNODE)               # MI_hDevNode
    v.mu.mem_write(INSTANCE, bytes(inst))
    cf = v.call(v.control_proc, {UC_X86_REG_EAX: 0x1B, UC_X86_REG_EBX: VM_HANDLE,
                                 UC_X86_REG_ECX: INSTANCE})
    check(cf == 0, 'Sys_Dynamic_Device_Init: mouse found')
    if cf:
        return
    flags = struct.unpack('<H', v.mu.mem_read(INSTANCE, 2))[0]
    check(flags & 0x0004 and not flags & 0x0008, 'instance flags detected (%04X)' % flags)
    want_irq = v.registry.get('IRQ', 7)
    check(v.virtualized and v.virtualized['irq'] == want_irq and v.unmasked,
          'IRQ %d virtualized and unmasked' % want_irq)
    check(v.mouse_data == dict(type=4, irq=want_irq, buttons=3, port=0),
          'VMD_Set_Mouse_Data %s' % v.mouse_data)
    rx = [e[2] for e in v.mouse.log if e[1] == 'rx']
    rate = v.registry.get('Rate', 40)
    check(rx == [0xFF, 0xF3, rate, 0xF4], 'mouse init %s' % [hex(b) for b in rx])
    t0 = v.t + 5000
    moves = [(3, -1, 0)] * 4 + [(0, 0, 1), (0, 0, 2), (0, 0, 4), (0, 0, 0), (-60, 30, 0)]
    for i, (dx, dy, b) in enumerate(moves):
        v.mouse.moves.append((t0 + i * 25000, dx, dy, b))
    v.idle(len(moves) * 25000 + 100000)
    btn = {0: 0, 1: 0x20, 2: 0x10, 4: 0x08}
    want = [(btn[b], dx, -dy, False) for dx, dy, b in moves]
    check(v.posted == want, 'VMD_Post_Pointer_Message calls: %d/%d %s' %
          (len(v.posted), len(want), v.posted[:2]))
    check(v.phys_eois >= len(moves), 'physical EOI per interrupt')
    disable = v.r32(INSTANCE + 0x1C)
    v.call(disable, {UC_X86_REG_ESI: INSTANCE})
    check(v.virtualized is None and v.r32(INSTANCE + 0x10) == 0, 'disable: IRQ given back')
    check((v.lpt_data, v.lpt_ctrl & 0x1F) == (0xAA, 0x0C), 'disable: port state restored')


if __name__ == '__main__':
    drv_standard_test()
    drv_standard_test(latency=1500.0)
    drv_enhanced_test()
    vxd_test('486-class CPU')
    vxd_test('slow 386, 1.5 ms interrupt latency', insn_us=0.3, io_us=1.5, latency=1500.0)
    vxd95_test('defaults from the INF')
    vxd95_test('LPT2, IRQ 5, rate 100, 1 ms latency', latency=1000.0,
               registry={'Port': 0x278, 'IRQ': 5, 'Rate': 100, 'Invert': 0})
    vxd95_test('no registry values', registry={})
    print()
    print('FAILED: %d\n  ' % len(FAILS) + '\n  '.join(FAILS) if FAILS else 'ALL PASSED')
    sys.exit(1 if FAILS else 0)
