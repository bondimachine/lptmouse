"""Extra scenarios: CPU speed, mouse clock speed, late IRQs, graphics cursors."""
import sys
import sim
from sim import Sim, check, FAILS, ROOT, driver_test
import os

def variant(label, insn, io, half, latency=0.0, tail='/V'):
    sim.US_PER_INSN, sim.US_PER_IO = insn, io
    sim.PS2Mouse.HALF = half
    print('#### %s' % label)
    n = len(FAILS)
    try:
        driver_test(tail, label, irq_latency_us=latency)
    except Exception as e:
        FAILS.append('%s: %r' % (label, e)); print('  EXC', e)
    sim.US_PER_INSN, sim.US_PER_IO, sim.PS2Mouse.HALF = 0.5, 1.0, 40.0

class Planar:
    """Minimal VGA planar memory: map mask (SEQ 2), read map (GC 4)."""
    def __init__(self, s):
        from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_HOOK_INSN
        from unicorn.x86_const import UC_X86_INS_IN, UC_X86_INS_OUT
        import random
        self.s = s
        self.planes = [bytearray(random.getrandbits(8) for _ in range(38400)) for _ in range(4)]
        self.seq = [0] * 8; self.gc = [0] * 16; self.si = 0; self.gi = 0
        self.seq[2] = 0x0F
        s.mu.hook_add(UC_HOOK_MEM_READ, self.rd, None, 0xA0000, 0xA0000 + 38399)
        s.mu.hook_add(UC_HOOK_MEM_WRITE, self.wr, None, 0xA0000, 0xA0000 + 38399)
        orig_out, orig_in = s._out, s._in
        def out(uc, port, size, value, ud):
            vals = [(port, value & 0xFF)] + ([(port + 1, value >> 8)] if size == 2 else [])
            for p, v in vals:
                if p == 0x3C4: self.si = v
                elif p == 0x3C5: self.seq[self.si & 7] = v
                elif p == 0x3CE: self.gi = v
                elif p == 0x3CF: self.gc[self.gi & 15] = v
                else: orig_out(uc, p, 1, v, ud)
        def inp(uc, port, size, ud):
            if port == 0x3C4: return self.si
            if port == 0x3C5: return self.seq[self.si & 7]
            if port == 0x3CE: return self.gi
            if port == 0x3CF: return self.gc[self.gi & 15]
            return orig_in(uc, port, size, ud)
        s._out, s._in = out, inp
        s.mu.hook_add(UC_HOOK_INSN, lambda uc, p, sz, v, ud: out(uc, p, sz, v, ud) if p in (0x3C4, 0x3C5, 0x3CE, 0x3CF) else None,
                      None, 1, 0, UC_X86_INS_OUT)
        self.inp = inp
    def rd(self, uc, access, addr, size, value, ud):
        off = addr - 0xA0000
        uc.mem_write(addr, bytes([self.planes[self.gc[4] & 3][off]]))
    def wr(self, uc, access, addr, size, value, ud):
        off = addr - 0xA0000
        for p in range(4):
            if self.seq[2] & (1 << p):
                self.planes[p][off] = value & 0xFF
    def snapshot(self):
        return [bytes(p) for p in self.planes]

def gfx12():
    print('#### graphics cursor, mode 12h with emulated planes')
    s = Sim()
    s.run_program(os.path.join(ROOT, 'lptmouse.com'), 0x1000, '')
    s.w8(0x449, 0x12)
    # unicorn calls every IN hook; route VGA reads through the planar model
    from unicorn import UC_HOOK_INSN
    from unicorn.x86_const import UC_X86_INS_IN
    v = Planar(s)
    s.mu.hook_add(UC_HOOK_INSN, lambda uc, p, sz, ud: v.inp(uc, p, sz, ud), None, 1, 0, UC_X86_INS_IN)
    before = v.snapshot()
    s.int33(0); s.int33(1)
    mid = v.snapshot()
    changed = sum(sum(1 for a, b in zip(x, y) if a != b) for x, y in zip(before, mid))
    check(changed > 0, 'planar cursor drawn (%d plane bytes changed)' % changed)
    # each plane: cursor pixels inverted under the XOR mask at the hotspot row 1
    s.int33(4, cx=3, dx=0); s.int33(4, cx=637, dx=470); s.int33(4, cx=100, dx=200)
    s.int33(2)
    check(v.snapshot() == before, 'all four planes restored after moves + hide')

def gfx(mode, label):
    print('#### graphics cursor, mode %02Xh (%s)' % (mode, label))
    s = Sim()
    reason, text = s.run_program(os.path.join(ROOT, 'lptmouse.com'), 0x1000, '')
    s.w8(0x449, mode)
    mem = bytes(s.mu.mem_read(0xA0000, 0x10000))
    s.int33(0)
    s.int33(1)
    after = bytes(s.mu.mem_read(0xA0000, 0x10000))
    changed = sum(1 for a, b in zip(mem, after) if a != b)
    check(changed > 0, 'cursor drawn (%d bytes changed)' % changed)
    s.int33(4, cx=0, dx=0)                     # move to the corner (clipping)
    s.int33(4, cx=639, dx=479)
    s.int33(2)
    final = bytes(s.mu.mem_read(0xA0000, 0x10000))
    check(final == mem, 'screen restored after moves + hide')

variant('XT speed (2.5 us/insn, 1.5 us/IO)', 2.5, 1.5, 40.0)
variant('fast mouse clock 16.7 kHz', 0.5, 1.0, 30.0)
variant('slow mouse clock 10 kHz, XT', 2.5, 1.5, 50.0)
variant('fast CPU (0.05 us/insn, 0.5 us/IO)', 0.05, 0.5, 30.0)
variant('poll mode on XT', 2.5, 1.5, 40.0, tail='/I0')
variant('XT, latency 2500 us (resend)', 2.5, 1.5, 40.0, latency=2500.0)
gfx(0x13, 'VGA 256')
gfx12()
print('\nFAILED: %d\n  ' % len(FAILS) + '\n  '.join(FAILS) if FAILS else '\nALL PASSED')
