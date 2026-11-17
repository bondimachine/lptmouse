# LPTMOUSE: a PS/2 mouse on the parallel port, no microcontroller

A cable with two diodes and two resistors connects a PS/2 mouse to a standard
PC parallel port. The PC speaks the PS/2 protocol itself by bit-banging the
port.

- `LPTMOUSE.COM` is a DOS mouse driver (INT 33h).
- `LPMTEST.COM` is a bring-up / diagnostic tool.
- `LPTMOUSE.DRV` and `LPTMOUSE.386` are the Windows 3.1 drivers (standard
  and 386 enhanced mode).
- `LPTMOUSE.VXD` with `LPTMOUSE.INF` is the native Windows 95 driver.

> Status: the DOS driver works on real hardware. The Windows drivers have
> only run in emulation so far, see *Testing*.

## The cable

PS/2 uses two open-collector lines, CLK and DATA, with pull-ups on the host
side. The parallel port data pins are push-pull outputs, so each one drives
its line through a Schottky diode: writing 0 pulls the line low, writing 1
lets it float (the diode blocks), so the mouse can still pull it low.
Two status inputs read the lines back.

```
 PC parallel port (DB-25)                          PS/2 socket (mini-DIN-6, female)

                          +5V ----+---------+--------------------- pin 4  +5V
                                  |         |
                                 R1        R2      R1, R2 = 4.7k
                                 4k7       4k7
                                  |         |
 pin 10  /ACK   <-----------------+---------)--------------------- pin 5  CLK
 pin 2   D0     -------|<---------+         |
                       D1                   |
 pin 13  SELECT <---------------------------+--------------------- pin 1  DATA
 pin 3   D1     -------|<-------------------+
                       D2
 pins 18-25 GND ---------------------+---------------------------- pin 3  GND
                                     |
                          GND of the +5V supply

 D1, D2 = Schottky diode (BAT85, BAT43, 1N5817, 1N5819).
          -|<-  the bar is the cathode (the band on the part): it goes
          toward the port pin, the anode toward the mouse line.
 Optional: 10 uF + 100 nF across +5V / GND at the PS/2 socket.
```

| DB-25 pin | Signal       | Direction | Connects to                     |
|-----------|--------------|-----------|---------------------------------|
| 2         | D0           | out       | CLK through diode D1            |
| 3         | D1           | out       | DATA through diode D2           |
| 10        | /ACK         | in        | CLK (also raises the port IRQ)  |
| 13        | SELECT       | in        | DATA                            |
| 18-25     | GND          |           | GND                             |

| Mini-DIN-6 pin | Signal |
|----------------|--------|
| 1              | DATA   |
| 3              | GND    |
| 4              | +5V    |
| 5              | CLK    |
| 2, 6           | unused |

Socket face (female, as seen when plugging the mouse in), with the key at the
top. Check it with a multimeter against the mouse plug before soldering; a PS/2
extension cable cut in half is the easiest way to get the socket:

```
     6  [key]  5
   4             3
      2       1
```

### Power

The parallel port cannot power a mouse (they draw up to ~100 mA). Take +5V
from any of these, with its ground tied to the parallel port ground:

- Game port (DA-15): pin 1 = +5V, pin 4 = GND.
- AT keyboard connector (DIN-5) through a pass-through: pin 5 = +5V, pin 4 = GND.
- A drive power connector (Molex): red = +5V, black = GND.
- A USB charger. Use it only together with the PC, so the mouse is never
  powered while the PC is off (the pull-ups would feed the port pins).

Connect the pull-ups R1 and R2 to the same +5V that powers the mouse.

### Variant with transistors (optional)

With the diodes the low level is about 0.5 to 0.7 V: the Schottky drop plus
the port's output low level. That is under the 0.8 V PS/2 limit but close
to it. If a mouse turns out to be marginal, replace each diode with an
N-channel MOSFET:

```
 D0 (pin 2) ---[100R]--- gate  2N7000   drain --- CLK
                                        source --- GND
 (same for D1 / DATA)
```

The transistor inverts the logic, so load the driver with `/X`. A 74HC07
(non-inverting open-drain buffer, powered from +5V) also works, without `/X`.

## Building

Needs NASM. The output is 8086 code, so it runs on anything from an XT up.

```bash
make
```

This produces `lptmouse.com` and `lpmtest.com`.

## Using it

1. In the BIOS setup, set the parallel port to Normal / SPP or bidirectional
   (EPP and ECP modes usually work too) and make sure its IRQ (7 for LPT1,
   5 for LPT2) is enabled and not used by a sound card.
2. Run `LPMTEST` first. It measures the port speed, checks the cable
   without the mouse involved, resets the mouse showing every reply, then
   prints each packet as you move the mouse (any key exits). `LPMTEST /W`
   just shows the two lines live, which helps with a multimeter.
3. Load the driver: `LPTMOUSE`.

```
LPTMOUSE [/Ln | /Pxxx] [/In] [/Rnn] [/X] [/V] [/U] [/S]
  /L1../L3  use LPTn (default LPT1)
  /Pxxx     port address in hex, e.g. /P378
  /In       IRQ of the port, 2-7 (default 7, 5 for 278h)
            /I0 = no IRQ, poll on the timer tick (18 Hz)
  /Rnn      sample rate 10,20,40,60,80,100,200 (default 40)
  /X        inverting line driver (transistors, 74LS05)
  /V        show the mouse replies while initialising
  /U        uninstall
  /S        show status and counters
```

`LPTMOUSE /S` prints how many packets were read, frame errors, dropped
bursts and resends. On a healthy setup the errors and drops stay at 0.
Resends count packets the driver had to ask for again because the interrupt
came late. A few are normal; if the cursor still lags in some program, try
`/I0`, which can't lose packets.

## Windows 3.1

Two files:

- `LPTMOUSE.DRV` is the Windows mouse driver (module name MOUSE, the
  standard Inquire / Enable / Disable / MouseGetIntVect interface).
  - In **standard mode** it reads the mouse itself, with the same code and
    the same interrupt trick as the DOS driver.
  - In **386 enhanced mode** it hands that job to the VxD.
- `LPTMOUSE.386` is a virtual device driver for enhanced mode. Enhanced mode
  traps every access to the LPT ports from normal code (the printer VxD
  owns them), and CLI there doesn't really disable interrupts. So
  bit-banging from the driver can't keep up with the mouse clock.
  - The VxD runs at ring 0, where port access is direct and CLI is real.
  - It takes over the port's IRQ and reads each packet when the IRQ fires.
  - It then raises a virtual IRQ in Windows. LPTMOUSE.DRV's interrupt
    handler collects the packets through the VxD's API.
  - Without the VxD, enhanced mode falls back to reading the port from the
    driver, which will probably drop packets.

The DOS driver is not needed under Windows. If it is loaded before Windows,
both drivers give the port back as they found it when Windows exits, so it
keeps working at the DOS prompt. Under enhanced mode, DOS boxes don't get a
mouse.

### Installing

1. Copy `LPTMOUSE.DRV` and `LPTMOUSE.386` to `C:\WINDOWS\SYSTEM`.
2. Edit `C:\WINDOWS\SYSTEM.INI`:

```ini
[boot]
mouse.drv=lptmouse.drv

[boot.description]
mouse.drv=Parallel port PS/2 mouse

[386Enh]
device=lptmouse.386

[LPTMouse]
Port=378
IRQ=7
Rate=40
Invert=0
```

The `[LPTMouse]` entries are all optional:

| Key      | Meaning                                                    |
|----------|------------------------------------------------------------|
| `Port`   | port address in hex (default: LPT1 from the BIOS)          |
| `IRQ`    | port IRQ (default 7, or 5 for port 278)                    |
| `Rate`   | sample rate: 10, 20, 40, 60, 80, 100 or 200 (default 40)   |
| `Invert` | 1 for the transistor / 74LS05 variant of the cable         |

Keep comments on their own lines in `SYSTEM.INI`. Windows 3.1 doesn't strip
a `;` comment written after a value.

Leave the existing `mouse=` line in `[386Enh]` as it is. Don't use Windows
Setup to change the mouse afterwards, because it rewrites `mouse.drv=`.

The Windows 3.1 drivers also load under Windows 95, but use the native
Windows 95 driver below there instead.

### Building

Needs NASM and [OpenWatcom v2](https://github.com/open-watcom/open-watcom-v2)
(only its linker, `wlink`, which produces the 16-bit NE and the LE VxD
formats). This builds the Windows 3.1 and Windows 95 files together. Unpack
the `ow-snapshot.tar.xz` release somewhere and run:

```bash
make windows WATCOM=/path/to/ow OWBIN=armo64
```

`OWBIN` is the snapshot's folder for your host: `armo64` (macOS arm64),
`bino64` (macOS x64), `binl64` (Linux x64) or `binnt64` (Windows x64). The
output goes to `build/`: `lptmouse.drv`, `lptmouse.386`, `lptmouse.vxd` and
`lptmouse.inf` (with DOS line endings).

### Notes

- The VxD uses device ID 3A7Dh, which is not registered with Microsoft.
  It's only used by LPTMOUSE.DRV to find the VxD's API.
- Only the IRQ mode exists under Windows: there is no timer polling mode
  like the DOS driver's `/I0`. The port's IRQ has to work.
- The driver reports 2 buttons. It also sends middle button events (the
  status bits 20h / 40h); Windows versions that don't know them ignore
  them.

## Windows 95

Windows 95 has its own mouse VxD, VMOUSE. It drives Windows 95's standard
MOUSE.DRV, handles acceleration, and gives DOS boxes their INT 33h mouse.
Extra pointing devices plug into it as "mini-drivers", and that's what
`LPTMOUSE.VXD` is.

- VMOUSE loads it for the mouse device created by `LPTMOUSE.INF`.
- On load, it resets the mouse, takes over the port's IRQ, and starts
  reading packets exactly like the other drivers (ring 0, interrupts off).
- It passes each packet to VMOUSE (`VMD_Post_Pointer_Message`).

Compared with running the Windows 3.1 drivers under 95:

- The mouse shows in Device Manager, installed from an INF.
- DOS boxes get the mouse too.
- Windows' own MOUSE.DRV is used, so mouse settings in Control Panel work.

### Installing

1. Put `LPTMOUSE.VXD` and `LPTMOUSE.INF` on a floppy or in a folder.
2. Control Panel, **Add New Hardware**, Next, answer **No** to detection,
   pick **Mouse**, then **Have Disk** and point it at that folder.
3. Choose "PS/2 mouse on the parallel port" and restart.
4. If Windows had another mouse installed, remove it in Device Manager.

The INF copies the VxD to `WINDOWS\SYSTEM`. It makes VMOUSE load the VxD
for this mouse, and sets `SYSTEM.INI` back to Windows' own `mouse.drv`. It
also removes a `device=lptmouse.386` line left from the Windows 3.1 setup.

### Settings

The settings are binary values in the mouse's driver key, which is
`HKEY_LOCAL_MACHINE\System\CurrentControlSet\Services\Class\Mouse\000n`
(the one whose `MouseDriver` is `lptmouse.vxd`). Edit them with REGEDIT, then
restart. Each is 4 bytes, low byte first:

| Value    | INF default    | Meaning                                        |
|----------|----------------|------------------------------------------------|
| `Port`   | `78 03 00 00`  | port address (378h)                            |
| `IRQ`    | `07 00 00 00`  | port IRQ                                       |
| `Rate`   | `28 00 00 00`  | sample rate (40): 10, 20, 40, 60, 80, 100, 200 |
| `Invert` | `00 00 00 00`  | 1 for the transistor / 74LS05 cable            |

To use LPT2, for example, set `Port` to `78 02 00 00` and `IRQ` to
`05 00 00 00`.

Windows 95 normally assigns the LPT port's IRQ to the printer port device.
The mouse doesn't claim any resources in Device Manager, so there's no
conflict warning. Just make sure the port's IRQ is enabled in the BIOS and
that nothing else (a sound card, say) uses it.

## How it works

The mouse generates the clock (10-16.7 kHz, so each half period is 30-50 us)
and the PC must catch every bit. An interrupt per clock edge is too slow and
too fragile, so the driver uses the PS/2 **inhibit** feature instead. While
the host holds CLK low the mouse may not send, and if CLK goes low in the
middle of a byte the mouse aborts that byte and sends it again later.

- **IRQ mode (default):** CLK is released while idle. The mouse's first
  clock edge raises the port IRQ (CLK is wired to /ACK). The handler holds
  CLK low to abort the transfer, releases it, and reads the whole packet in a
  tight polling loop with interrupts off. The interrupt latency doesn't
  matter, because the mouse restarts the byte.
  - If the interrupt arrives so late that the first byte was already
    complete, the handler only gets part of the packet. It then sends the
    PS/2 Resend command (FE) and reads the packet again.
  - If the whole packet was already sent before the handler ran (something
    kept interrupts off for longer than a packet takes, as Leisure Suit
    Larry 2's interpreter does on an XT), the handler reads nothing. It then
    also sends Resend. The mouse only keeps its last packet, so if several
    packets went by unanswered, the earlier ones are still lost.
- **Poll mode (`/I0`):** CLK is held low all the time. On each timer tick
  the driver releases it briefly and reads whatever the mouse has. No IRQ is
  needed, but the cursor only updates 18 times a second.

All timeouts are counted in port reads. At startup the driver measures how
many reads fit in a millisecond and scales them, so the same binary works
on an XT and on a Pentium.

**CPU cost:** each 3-byte packet takes about 3 ms with interrupts off. At the
default 40 packets/s that's roughly 12% of the CPU while the mouse moves, and
nothing when it doesn't. Programs that need very low interrupt latency
(serial transfers at high speed without a FIFO, for example) can lose
characters while the mouse is moving. A lower `/R` rate reduces the load.

### Driver features

- **INT 33h functions:** 00-0Ch, 0Fh, 10h (simplified to "hide"), 13h-17h,
  1Ah, 1Bh, 1Dh-21h, 24h, 26h. That covers position, buttons, ranges,
  cursor shapes, the event handler, mickey counters, state save/restore and
  enable/disable.
- **Cursor drawing:**
  - Text modes: 40/80 columns, 25-50 rows, colour and mono.
  - CGA mode 6, mode 11h and mode 13h.
  - EGA/VGA 16-colour modes 0Dh, 0Eh, 10h and 12h. These need a VGA card,
    because the cursor code has to read back registers that an EGA can't
    report.
  - Modes 4, 5 and 0Fh report coordinates but draw no cursor.
- **Not implemented:** acceleration (double-speed threshold) and
  sensitivity, which are stored but not applied, light pen emulation, and
  the wheel.
- **Windows:** not through this driver. Windows uses its own MOUSE.DRV and
  never calls INT 33h; see *Windows 3.1* and *Windows 95* above.

## Testing

`test/sim.py` runs the real `.COM` files in a CPU emulator
([Unicorn](https://www.unicorn-engine.org/)) with a bit-level PS/2 mouse
simulation wired to the emulated port. The simulated mouse generates the
clock, honours inhibit (aborting and retransmitting), clocks in host
commands and acks them. The harness also emulates the 8259 interrupt
controller, the timer tick and a minimal DOS. It checks install, reset,
cursor drawing, movement, buttons, counters, `/S`, `/U` and LPMTEST.

`test/stress.py` repeats the driver checks under harder conditions:

- XT speed, and a fast CPU.
- The fastest and slowest PS/2 clocks (16.7 kHz and 10 kHz).
- 300 us to 2.5 ms of interrupt latency.
- Graphics cursors in mode 13h, and in mode 12h with emulated VGA planes.

`test/winsim.py` covers the Windows drivers without Windows:

- `LPTMOUSE.DRV` is loaded by a small NE loader with stub KERNEL imports.
  - **Standard mode:** it reads the simulated mouse itself. The test checks
    LibEntry, Inquire, Enable, the events passed to Windows, and Disable.
  - **Enhanced mode:** it talks to a fake VxD. The test also checks that the
    driver never touches the LPT ports in that mode.
- `LPTMOUSE.386` is loaded by a small LE loader. It runs with fake VMM and
  VPICD services and the simulated mouse. The test checks:
  - start, IRQ virtualization and mouse reset;
  - packets delivered through the virtual IRQ, including on a slow 386
    with 1.5 ms interrupt latency;
  - the port restored on exit.
- `LPTMOUSE.VXD` is loaded the same way, with fake VMOUSE and Configuration
  Manager services. The test sends `Sys_Dynamic_Device_Init` with a mouse
  instance, like VMOUSE does, then checks:
  - the registry settings are used, with defaults when they're missing;
  - the instance flags and `VMD_Set_Mouse_Data`;
  - every packet reaches `VMD_Post_Pointer_Message` with VMOUSE's button
    bits and Y direction, including with 1 ms interrupt latency;
  - disable gives the IRQ and the port back.

```bash
pip install unicorn
make
make windows WATCOM=/path/to/ow
python test/sim.py
python test/stress.py
python test/winsim.py
```

Things only real hardware can confirm:

- How quickly real mice resume sending after an inhibit. The driver waits
  up to 3 ms.
- Whether a given mouse answers Resend (FE) with the last packet. The driver
  accepts an FA ack before the packet too.
- That a given port raises its IRQ on /ACK edges.
- The diode low level with a given port chipset.
