"""Runs the DOS programs in a CPU emulator (Unicorn) with a simulated PS/2
mouse wired to the parallel port like the real cable.

Simulated time: every instruction costs US_PER_INSN microseconds and every
port access US_PER_IO (an ISA port read takes about 1 us on real hardware).
The mouse is modelled at the bit level: it generates the clock (12.5 kHz),
honours host inhibit (aborts and retransmits the byte), detects the host's
request to send, clocks in commands and acks them.

Usage: python sim.py   (needs `pip install unicorn`; run `make` first)
"""
import collections
import os
import sys

from unicorn import Uc, UC_ARCH_X86, UC_MODE_16, UC_HOOK_CODE, UC_HOOK_INTR, UC_HOOK_INSN
from unicorn.x86_const import *

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

US_PER_INSN = 0.5
US_PER_IO = 1.0
TICK_US = 54925.0

LPT = 0x378
BIOS_SEG = 0xF000
IDLE_IP = 0x1000        # sti; jmp $
RET_IP = 0x2000         # return address for calls made by the harness


class PS2Mouse:
    HALF = 40.0          # clock half period (12.5 kHz)

    def __init__(self, sim):
        self.sim = sim
        self.dev_clk_low = False
        self.dev_data_low = False
        self.queue = collections.deque()
        self.scheduled = []      # (time, [bytes])
        self.moves = []          # (time, dx, dy, buttons)
        self.streaming = False
        self.rate = 100
        self.param_for = None
        self.log = []
        self.aborts = 0
        self.last_packet = []
        self.ack_resend = False      # some mice send FA before the packet
        self.wake = 0.0
        self.now = 0.0
        self.gen = self.main()

    # line levels (True = high)
    def clk(self):
        return not (self.dev_clk_low or self.sim.host_clk_low)

    def data(self):
        return not (self.dev_data_low or self.sim.host_data_low)

    def update(self, t):
        while self.wake <= t:
            self.now = self.wake
            if self.scheduled:
                for item in list(self.scheduled):
                    if item[0] <= self.now:
                        self.queue.extend(item[1])
                        self.scheduled.remove(item)
            if self.moves and self.moves[0][0] <= self.now:
                _, dx, dy, b = self.moves.pop(0)
                if self.streaming:
                    b0 = 0x08 | b | (0x10 if dx < 0 else 0) | (0x20 if dy < 0 else 0)
                    self.last_packet = [b0, dx & 0xFF, dy & 0xFF]
                    self.queue.extend(self.last_packet)
            self.wake += next(self.gen)
            self.sim.check_edge()

    def main(self):
        while True:
            if self.clk() and self.sim.host_data_low and not self.dev_data_low:
                yield from self.receive()
                continue
            if self.queue and self.clk() and self.data():
                ok = True
                for _ in range(5):          # CLK must be high for 50 us
                    yield 10.0
                    if not (self.clk() and self.data()):
                        ok = False
                        break
                if not ok:
                    continue
                if (yield from self.send(self.queue[0])):
                    self.log.append((self.now, 'tx', self.queue.popleft()))
                    yield 30.0
                else:
                    self.aborts += 1
                continue
            yield 10.0

    def send(self, b):
        par = 1 ^ (bin(b).count('1') & 1)
        bits = [0] + [(b >> i) & 1 for i in range(8)] + [par, 1]
        for bit in bits:
            if self.sim.host_clk_low:
                return (yield from self.abort())
            self.dev_data_low = bit == 0
            yield 15.0
            if self.sim.host_clk_low:
                return (yield from self.abort())
            self.dev_clk_low = True
            yield self.HALF
            self.dev_clk_low = False
            yield self.HALF - 15.0
        self.dev_data_low = False
        return True

    def abort(self):
        self.dev_data_low = False
        self.dev_clk_low = False
        while self.sim.host_clk_low:
            yield 10.0
        return False

    def receive(self):
        yield 50.0
        bits = []
        for _ in range(10):
            self.dev_clk_low = True
            yield self.HALF
            self.dev_clk_low = False
            bits.append(0 if self.sim.host_data_low else 1)   # rising edge
            yield self.HALF
        self.dev_data_low = True            # ack bit
        yield 15.0
        self.dev_clk_low = True
        yield self.HALF
        self.dev_clk_low = False
        yield self.HALF
        self.dev_data_low = False
        b = sum(bits[i] << i for i in range(8))
        ok = bits[9] == 1 and sum(bits[:9]) % 2 == 1
        self.log.append((self.now, 'rx', b, ok))
        if not ok:
            self.queue.append(0xFE)
            return
        self.command(b)

    def command(self, b):
        q = self.queue
        if self.param_for is not None:
            if self.param_for == 0xF3:
                self.rate = b
            self.param_for = None
            q.append(0xFA)
        elif b == 0xFF:
            q.clear()
            self.streaming = False
            q.append(0xFA)
            self.scheduled.append((self.now + 400000.0, [0xAA, 0x00]))
        elif b in (0xF3, 0xE8):
            self.param_for = b
            q.append(0xFA)
        elif b == 0xF4:
            self.streaming = True
            q.append(0xFA)
        elif b == 0xF5:
            self.streaming = False
            q.append(0xFA)
        elif b == 0xF2:
            q.extend([0xFA, 0x00])
        elif b == 0xFE:
            if self.ack_resend:
                q.append(0xFA)
            q.extend(self.last_packet)
        else:
            q.append(0xFA)


class Sim:
    def __init__(self, irq_latency_us=0.0):
        self.mu = Uc(UC_ARCH_X86, UC_MODE_16)
        self.mu.mem_map(0, 0x110000)
        self.t = 0.0
        self.next_tick = TICK_US
        self.lpt_data = 0xFF
        self.lpt_ctrl = 0x0C
        self.host_clk_low = False
        self.host_data_low = False
        self.prev_clk = True
        self.irr = 0
        self.isr = 0
        self.imr = 0xBC              # IRQ 0, 1, 6 enabled like a BIOS would
        self.read_isr = False
        self.irq_latency_us = irq_latency_us
        self.irq7_raised_at = None
        self.out = []
        self.keys = []               # (time, scancode/ascii word)
        self.stop_reason = None
        self.until_t = None
        self.mouse = PS2Mouse(self)
        self.edges = 0
        self._setup_bios()
        mu = self.mu
        mu.hook_add(UC_HOOK_CODE, self._code)
        mu.hook_add(UC_HOOK_INTR, self._intr)
        mu.hook_add(UC_HOOK_INSN, self._in, None, 1, 0, UC_X86_INS_IN)
        mu.hook_add(UC_HOOK_INSN, self._out, None, 1, 0, UC_X86_INS_OUT)

    # --- memory helpers ---
    def w8(self, a, v): self.mu.mem_write(a, bytes([v & 0xFF]))
    def w16(self, a, v): self.mu.mem_write(a, (v & 0xFFFF).to_bytes(2, 'little'))
    def r8(self, a): return self.mu.mem_read(a, 1)[0]
    def r16(self, a): return int.from_bytes(self.mu.mem_read(a, 2), 'little')

    def _setup_bios(self):
        b = BIOS_SEG * 16
        self.mu.mem_write(b, b'\xCF')                       # iret
        # INT 8: increment the tick count, EOI
        self.mu.mem_write(b + 0x20, bytes([0x50, 0x1E, 0x31, 0xC0, 0x8E, 0xD8,
                                           0xFF, 0x06, 0x6C, 0x04, 0xB0, 0x20,
                                           0xE6, 0x20, 0x1F, 0x58, 0xCF]))
        self.mu.mem_write(b + IDLE_IP, b'\xFB\xEB\xFE')     # sti; jmp $
        self.mu.mem_write(b + RET_IP, b'\xF4')
        for v in range(256):
            self.w16(v * 4, 0)
            self.w16(v * 4 + 2, BIOS_SEG)
        self.w16(8 * 4, 0x20)
        # BIOS data area
        self.w16(0x408, LPT)
        self.w8(0x449, 3)
        self.w16(0x44A, 80)
        self.w16(0x44E, 0)
        self.w8(0x484, 24)
        for i in range(80 * 25):
            self.w16(0xB8000 + i * 2, 0x0720)

    # --- lines / IRQ ---
    def _lines_from_port(self):
        self.host_clk_low = (self.lpt_data & 1) == 0
        self.host_data_low = (self.lpt_data & 2) == 0

    def check_edge(self):
        clk = self.mouse.clk()
        if clk and not self.prev_clk:
            self.edges += 1
            if self.lpt_ctrl & 0x10:
                if not (self.irr & 0x80):
                    self.irq7_raised_at = self.t
                self.irr |= 0x80
        self.prev_clk = clk

    def _status(self):
        return 0x08 | (0x40 if self.mouse.clk() else 0) | (0x10 if self.mouse.data() else 0)

    def _deliverable(self):
        pend = self.irr & ~self.imr & 0xFF
        for irq in range(8):
            if self.isr & (1 << irq):
                return None
            if pend & (1 << irq):
                if irq == 7 and self.irq7_raised_at is not None and \
                        self.t - self.irq7_raised_at < self.irq_latency_us:
                    continue
                return irq
        return None

    # --- hooks ---
    def _code(self, uc, addr, size, ud):
        self.t += US_PER_INSN
        if self.mouse.wake <= self.t:
            self.mouse.update(self.t)
        if self.t >= self.next_tick:
            self.irr |= 1
            self.next_tick += TICK_US
        if self.until_t is not None and self.t >= self.until_t:
            self.stop_reason = 'time'
            uc.emu_stop()
            return
        if self.irr and uc.reg_read(UC_X86_REG_EFLAGS) & 0x200:
            if self._deliverable() is not None:
                self.stop_reason = 'irq'
                uc.emu_stop()

    def _in(self, uc, port, size, ud):
        self.t += US_PER_IO
        self.mouse.update(self.t)
        if port == LPT:
            return self.lpt_data
        if port == LPT + 1:
            return self._status()
        if port == LPT + 2:
            return self.lpt_ctrl
        if port == 0x20:
            return self.isr if self.read_isr else self.irr
        if port == 0x21:
            return self.imr
        return 0xFF

    def _out(self, uc, port, size, value, ud):
        self.t += US_PER_IO
        self.mouse.update(self.t)
        if port == LPT:
            self.lpt_data = value & 0xFF
            self._lines_from_port()
            self.check_edge()
        elif port == LPT + 2:
            self.lpt_ctrl = value & 0xFF
        elif port == 0x20:
            v = value & 0xFF
            if v == 0x20:
                for i in range(8):
                    if self.isr & (1 << i):
                        self.isr &= ~(1 << i)
                        break
            elif v == 0x0B:
                self.read_isr = True
            elif v == 0x0A:
                self.read_isr = False
        elif port == 0x21:
            self.imr = value & 0xFF

    def _push(self, v):
        sp = (self.mu.reg_read(UC_X86_REG_SP) - 2) & 0xFFFF
        self.mu.reg_write(UC_X86_REG_SP, sp)
        self.w16(self.mu.reg_read(UC_X86_REG_SS) * 16 + sp, v)

    def _vector(self, vec):
        mu = self.mu
        fl = mu.reg_read(UC_X86_REG_EFLAGS)
        self._push(fl)
        self._push(mu.reg_read(UC_X86_REG_CS))
        self._push(mu.reg_read(UC_X86_REG_IP))
        mu.reg_write(UC_X86_REG_EFLAGS, fl & ~0x300)
        mu.reg_write(UC_X86_REG_CS, self.r16(vec * 4 + 2))
        mu.reg_write(UC_X86_REG_IP, self.r16(vec * 4))

    def _set_cf(self, on):
        fl = self.mu.reg_read(UC_X86_REG_EFLAGS)
        self.mu.reg_write(UC_X86_REG_EFLAGS, (fl | 1) if on else (fl & ~1))

    def _intr(self, uc, intno, ud):
        r = uc.reg_read
        ah = (r(UC_X86_REG_AX) >> 8) & 0xFF
        al = r(UC_X86_REG_AX) & 0xFF
        if intno == 0x21:
            if ah == 0x02:
                self.out.append(chr(r(UC_X86_REG_DX) & 0xFF))
            elif ah == 0x25:
                self.w16(al * 4, r(UC_X86_REG_DX))
                self.w16(al * 4 + 2, r(UC_X86_REG_DS))
            elif ah == 0x35:
                uc.reg_write(UC_X86_REG_BX, self.r16(al * 4))
                uc.reg_write(UC_X86_REG_ES, self.r16(al * 4 + 2))
            elif ah == 0x49:
                self._set_cf(False)
            elif ah in (0x31, 0x4C):
                self.stop_reason = 'tsr' if ah == 0x31 else 'exit'
                self.exit_code = al
                self.tsr_paras = r(UC_X86_REG_DX)
                uc.emu_stop()
            else:
                raise RuntimeError('INT 21h AH=%02X' % ah)
        elif intno == 0x16:
            due = [k for k in self.keys if k[0] <= self.t]
            fl = uc.reg_read(UC_X86_REG_EFLAGS)
            if ah == 0x01:
                if due:
                    uc.reg_write(UC_X86_REG_AX, due[0][1])
                    uc.reg_write(UC_X86_REG_EFLAGS, fl & ~0x40)
                else:
                    uc.reg_write(UC_X86_REG_EFLAGS, fl | 0x40)
            elif ah == 0x00:
                if due:
                    uc.reg_write(UC_X86_REG_AX, due[0][1])
                    self.keys.remove(due[0])
        else:
            # real vectoring through the IVT (driver hooks, INT 33h, ...)
            self._vector(intno)
            self.stop_reason = 'vector'
            uc.emu_stop()

    # --- running ---
    def run(self, until_t=None, until_lin=None):
        self.until_t = until_t
        mu = self.mu
        while True:
            if self.stop_reason == 'irq':
                irq = self._deliverable()
                if irq is not None:
                    self.irr &= ~(1 << irq)
                    if irq == 7:
                        self.irq7_raised_at = None
                    self.isr |= 1 << irq
                    self._vector(8 + irq)
            self.stop_reason = None
            lin = mu.reg_read(UC_X86_REG_CS) * 16 + mu.reg_read(UC_X86_REG_IP)
            if until_lin is not None and lin == until_lin:
                return 'until'
            mu.emu_start(lin, until_lin if until_lin is not None else 0x1FFFFF)
            if self.stop_reason in ('tsr', 'exit', 'time'):
                return self.stop_reason
            if self.stop_reason is None:
                lin = mu.reg_read(UC_X86_REG_CS) * 16 + mu.reg_read(UC_X86_REG_IP)
                if until_lin is not None and lin == until_lin:
                    return 'until'
                raise RuntimeError('emulation stopped at %05X' % lin)

    def load_com(self, path, seg, tail=''):
        data = open(path, 'rb').read()
        base = seg * 16
        self.mu.mem_write(base, b'\x00' * 0x100)
        self.mu.mem_write(base, b'\xCD\x20')
        tail = (' ' + tail) if tail else ''
        self.w8(base + 0x80, len(tail))
        self.mu.mem_write(base + 0x81, tail.encode() + b'\r')
        self.w16(base + 0x2C, 0x0F00)
        self.mu.mem_write(base + 0x100, data)
        mu = self.mu
        for reg in (UC_X86_REG_CS, UC_X86_REG_DS, UC_X86_REG_ES, UC_X86_REG_SS):
            mu.reg_write(reg, seg)
        mu.reg_write(UC_X86_REG_SP, 0xFFFE)
        self.w16(base + 0xFFFE, 0)
        mu.reg_write(UC_X86_REG_IP, 0x100)
        mu.reg_write(UC_X86_REG_EFLAGS, 0x202)
        self.out = []

    def run_program(self, path, seg, tail='', max_us=5e6):
        self.load_com(path, seg, tail)
        reason = self.run(until_t=self.t + max_us)
        return reason, ''.join(self.out).replace('\r', '')

    def idle(self, us):
        mu = self.mu
        mu.reg_write(UC_X86_REG_CS, BIOS_SEG)
        mu.reg_write(UC_X86_REG_IP, IDLE_IP)
        mu.reg_write(UC_X86_REG_SS, 0x9000)
        mu.reg_write(UC_X86_REG_SP, 0xFFF0)
        mu.reg_write(UC_X86_REG_EFLAGS, 0x202)
        self.run(until_t=self.t + us)

    def int33(self, ax, bx=0, cx=0, dx=0, es=0):
        mu = self.mu
        mu.reg_write(UC_X86_REG_SS, 0x9000)
        mu.reg_write(UC_X86_REG_SP, 0xFFF0)
        for reg, v in ((UC_X86_REG_AX, ax), (UC_X86_REG_BX, bx), (UC_X86_REG_CX, cx),
                       (UC_X86_REG_DX, dx), (UC_X86_REG_ES, es), (UC_X86_REG_DS, 0x9000)):
            mu.reg_write(reg, v)
        mu.reg_write(UC_X86_REG_CS, BIOS_SEG)
        mu.reg_write(UC_X86_REG_IP, RET_IP)
        mu.reg_write(UC_X86_REG_EFLAGS, 0x202)
        self._vector(0x33)
        self.run(until_lin=BIOS_SEG * 16 + RET_IP)
        r = mu.reg_read
        return dict(ax=r(UC_X86_REG_AX), bx=r(UC_X86_REG_BX), cx=r(UC_X86_REG_CX),
                    dx=r(UC_X86_REG_DX), es=r(UC_X86_REG_ES))

    def move(self, at_us, dx, dy, buttons=0):
        self.mouse.moves.append((at_us, dx, dy, buttons))
        self.mouse.moves.sort()


# ---------------------------------------------------------------------------
FAILS = []


def check(cond, what):
    print(('  ok   ' if cond else '  FAIL ') + what)
    if not cond:
        FAILS.append(what)


def s16(v):
    return v - 0x10000 if v & 0x8000 else v


def driver_test(tail, label, irq_latency_us=0.0, ack_resend=False):
    print('== driver %s (%s)' % (label, tail))
    sim = Sim(irq_latency_us)
    sim.mouse.ack_resend = ack_resend
    drv = os.path.join(ROOT, 'lptmouse.com')
    reason, text = sim.run_program(drv, 0x1000, tail)
    print('   ' + text.strip().replace('\n', '\n   '))
    check(reason == 'tsr', 'driver stays resident')
    if reason != 'tsr':
        return
    rx = [e[2] for e in sim.mouse.log if e[1] == 'rx']
    check(rx[:1] == [0xFF] and 0xF4 in rx, 'mouse got reset and enable: %s' %
          ' '.join('%02X' % b for b in rx))

    r = sim.int33(0)
    check(r['ax'] == 0xFFFF and r['bx'] == 2, 'fn 0 reset -> AX=FFFF BX=2')
    r = sim.int33(3)
    check((r['cx'], r['dx']) == (320, 96), 'starts centred: %d,%d' % (r['cx'], r['dx']))
    sim.int33(1)                                      # show cursor
    cell = 0xB8000 + (12 * 80 + 40) * 2
    check(sim.r16(cell) == 0x7020, 'text cursor drawn at 40,12 (%04X)' % sim.r16(cell))

    t0 = sim.t + 5000
    for i in range(10):
        sim.move(t0 + i * 25000, 4, -2)             # right and down
    sim.move(t0 + 10 * 25000, 0, 0, 1)              # left button down
    sim.move(t0 + 11 * 25000, 0, 0, 0)              # up
    sim.move(t0 + 12 * 25000, -200, 150, 2)         # big move left/up, right button
    sim.move(t0 + 13 * 25000, 0, 0, 0)
    sim.idle(14 * 25000 + 300000)

    r = sim.int33(3)
    x, y = r['cx'], r['dx']
    # 10*4 = 40 mickeys right -> 40 px; 10*2 down at 16 mickeys/8 px -> 10 px
    # then -200 -> 200 px left; +150 up -> 75 px up
    check((x, y) == (160, 32), 'position after moves: %d,%d (want 160,32)' % (x, y))
    r = sim.int33(5, bx=0)
    check(r['bx'] == 1 and (r['cx'], r['dx']) == (360, 104),
          'left press count 1 at 360,104: %d at %d,%d' % (r['bx'], r['cx'], r['dx']))
    r = sim.int33(6, bx=0)
    check(r['bx'] == 1, 'left release count 1')
    r = sim.int33(5, bx=1)
    check(r['bx'] == 1, 'right press count 1')
    r = sim.int33(0x0B)
    check((s16(r['cx']), s16(r['dx'])) == (40 - 200, 20 - 150),
          'mickey counters %d,%d' % (s16(r['cx']), s16(r['dx'])))
    cell = 0xB8000 + ((32 // 8) * 80 + 160 // 8) * 2
    check(sim.r16(cell) == 0x7020, 'text cursor followed to 20,4')
    old = 0xB8000 + (12 * 80 + 40) * 2
    check(sim.r16(old) == 0x0720, 'old cursor cell restored')

    reason, text = sim.run_program(drv, 0x3000, '/S')
    print('   ' + text.strip().replace('\n', '\n   '))
    check('frame errors: 0' in text, 'no frame errors')
    reason, text = sim.run_program(drv, 0x3000, '/U')
    check('removed' in text, 'uninstall: ' + text.strip().splitlines()[-1])
    print('   mouse aborts (inhibit/retransmit): %d, CLK edges: %d' %
          (sim.mouse.aborts, sim.edges))


HANDLER_SEG = 0x5000


def handler_test(tail, label, irq_latency_us=0.0):
    """Event handler (fn 0Ch) the way Sierra's SCI0 interpreter uses it:
    reset in EGA mode 0Dh, read the position, then a handler for movement
    and left button press/release that never shows the driver's cursor."""
    print('== event handler %s (%s)' % (label, tail))
    sim = Sim(irq_latency_us)
    drv = os.path.join(ROOT, 'lptmouse.com')
    reason, text = sim.run_program(drv, 0x1000, tail)
    check(reason == 'tsr', 'driver stays resident')
    if reason != 'tsr':
        return
    h = open(os.path.join(HERE, 'fixtures', 'handler.bin'), 'rb').read()
    sim.mu.mem_write(HANDLER_SEG * 16, h)
    sim.w8(0x449, 0x0D)
    r = sim.int33(0)
    check(r['ax'] == 0xFFFF, 'fn 0 reset')
    r = sim.int33(3)
    check(r['cx'] >= 300, 'mode 0Dh x range is 0-639 (centre %d)' % r['cx'])
    sim.int33(0x0C, cx=7, dx=0, es=HANDLER_SEG)

    t0 = sim.t + 5000
    for i in range(20):
        sim.move(t0 + i * 10000, 3, -1)
    sim.move(t0 + 20 * 10000, 0, 0, 1)
    sim.move(t0 + 21 * 10000, 2, 0, 1)            # drag
    sim.move(t0 + 22 * 10000, 0, 0, 0)
    sim.move(t0 + 23 * 10000, 0, 0, 2)            # right: not in the mask
    sim.move(t0 + 24 * 10000, 0, 0, 0)
    sim.idle(25 * 10000 + 300000)

    base = HANDLER_SEG * 16
    n = sim.r16(base + 0x70)
    log = []
    for i in range(min(n, 64)):
        e = base + 0x80 + i * 16
        log.append(tuple(sim.r16(e + k) for k in range(0, 16, 2)))
    masks = [e[0] for e in log]
    print('   %d calls, masks %s' % (n, ' '.join('%X' % m for m in masks)))
    check(n == 23, 'handler called for 21 moves + press + release (%d)' % n)
    check(masks.count(2) == 1 and masks.count(4) == 1, 'one left press, one release')
    check(all(m & ~7 == 0 for m in masks), 'only masked events reported')
    press = [e for e in log if e[0] & 2]
    rel = [e for e in log if e[0] & 4]
    if press and rel:
        check(press[0][1] == 1 and rel[0][1] == 0, 'BX button state on press/release')
    last = log[-1] if log else (0,) * 8
    r = sim.int33(3)
    check((r['cx'], r['dx']) == (last[2], last[3]),
          'handler CX,DX match fn 3: %d,%d vs %d,%d' % (last[2], last[3], r['cx'], r['dx']))
    sps = sorted(set((e[6], e[7]) for e in log))
    print('   handler SS:SP seen: %s' % ' '.join('%04X:%04X' % s for s in sps))
    reason, text = sim.run_program(drv, 0x3000, '/S')
    print('   ' + text.strip().replace('\n', '\n   '))
    check('frame errors: 0' in text, 'driver still healthy afterwards')


def lpmtest_test():
    print('== lpmtest')
    sim = Sim()
    sim.keys.append((1.6e6, 0x011B))
    for i in range(3):
        sim.move(1.2e6 + i * 30000, 3, -1, 1 if i == 1 else 0)
    reason, text = sim.run_program(os.path.join(ROOT, 'lpmtest.com'), 0x1000, '/P378',
                                   max_us=3e6)
    print('   ' + text.strip().replace('\n', '\n   '))
    check(reason == 'exit', 'exits on a key')
    check('FA AA 00 FA FA FA' in text, 'mouse replies shown')
    check('total 9,-3' in text, 'packets decoded')


if __name__ == '__main__':
    driver_test('/V', 'IRQ 7')
    driver_test('/V', 'IRQ 7, 300 us interrupt latency', irq_latency_us=300.0)
    driver_test('/V', 'IRQ 7, 1500 us latency (resend)', irq_latency_us=1500.0)
    driver_test('/V', 'IRQ 7, 1500 us latency (resend, FA first)', irq_latency_us=1500.0,
                ack_resend=True)
    driver_test('/V', 'IRQ 7, 6 ms latency (whole packet missed)', irq_latency_us=6000.0)
    driver_test('/I0', 'timer polling')
    handler_test('', 'IRQ 7')
    handler_test('', 'IRQ 7, 1500 us latency', irq_latency_us=1500.0)
    handler_test('', 'IRQ 7, 6 ms latency (like LSL2)', irq_latency_us=6000.0)
    handler_test('/I0', 'timer polling')
    lpmtest_test()
    print()
    print('FAILED: %d' % len(FAILS) if FAILS else 'ALL PASSED')
    sys.exit(1 if FAILS else 0)
