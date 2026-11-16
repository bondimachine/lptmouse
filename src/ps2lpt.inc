; ps2lpt.inc - PS/2 device protocol bit-banged on a PC parallel port.
;
; Wiring (see README.md):
;   D0  (pin 2)  -> drives PS/2 CLK low  (Schottky diode, cathode at the port)
;   D1  (pin 3)  -> drives PS/2 DATA low (Schottky diode, cathode at the port)
;   /ACK (pin 10) <- PS/2 CLK   (status bit 6, also raises the port IRQ)
;   SELECT (pin 13) <- PS/2 DATA (status bit 4)
;
; Writing 0 to D0/D1 pulls the line low, 1 releases it (open-collector
; emulation). With an inverting driver (2N7000 / 74LS05) set drive_xor = 03h.
;
; The includer defines (in the segment DS points to):
;   port_data   dw  base port (378h...)
;   port_status dw  base + 1
;   drive_xor   db  0 (diodes / 74HC07) or 03h (inverting driver)
;
; Timeouts are counted in status port reads (polling loop iterations) and
; calibrated at startup, see calibrate_timeouts in ps2init.inc. The PS/2 clock runs at 10-16.7 kHz (30-50 us half period).
;
; All routines must run with interrupts disabled (except mouse init waits).
; 8086 instructions only.

STAT_CLK    equ 40h     ; status bit 6: /ACK, pin 10
STAT_DATA   equ 10h     ; status bit 4: SELECT, pin 13

OUT_IDLE    equ 0FFh    ; CLK and DATA released
OUT_CLKLO   equ 0FEh    ; CLK held low (inhibit), DATA released
OUT_DATLO   equ 0FDh    ; DATA low, CLK released
OUT_BOTHLO  equ 0FCh

; Timeouts are variables (t_xxx, in port reads) set by calibrate_timeouts
; from the measured port speed; the defaults assume 1 read per microsecond.

%include "bits.inc"

; wait until (status & %1) == 0, at most %2 reads, else jump to %3.
; DX = status port. Clobbers AL, CX. AL holds the last status read.
%macro WAITLO 3
        SETCNT %2
%%l:    in al, dx
        test al, %1
        jz %%ok
        loop %%l
        jmp %3
%%ok:
%endmacro

; wait until (status & %1) != 0
%macro WAITHI 3
        SETCNT %2
%%l:    in al, dx
        test al, %1
        jnz %%ok
        loop %%l
        jmp %3
%%ok:
%endmacro

; burn %1 port reads (DX = any readable port). Clobbers AL, CX.
%macro DELAY 1
        SETCNT %1
%%l:    in al, dx
        loop %%l
%endmacro

; set the CLK/DATA drive bits: AL = OUT_xxx. DX = data port.
%macro LINES 1
        mov al, %1
        xor al, [drive_xor]
        out dx, al
%endmacro

; ---------------------------------------------------------------------------
; ps2_lines: AL = OUT_xxx value, written to the data port. Keeps DX.
; ---------------------------------------------------------------------------
ps2_lines:
        push dx
        mov dx, [port_data]
        xor al, [drive_xor]
        out dx, al
        pop dx
        ret

; ---------------------------------------------------------------------------
; ps2_recv: receive one byte from the device.
;   in:  DX = status port, CX (ECX) = reads to wait for the start bit,
;        CLK released
;   out: CF=0, AL = byte
;        CF=1, AH = 1 no start bit, 2 framing/timeout, 3 parity error
;   Clobbers CX, AL/AH.
; ---------------------------------------------------------------------------
ps2_recv:
        push bx
.ws:    in al, dx
        test al, STAT_CLK
        jz .start
        loop .ws
        mov ah, 1
        jmp .fail
.start:
        test al, STAT_DATA          ; start bit must be 0
        jnz .frame
        WAITHI STAT_CLK, [t_bit], .frame
        xor bx, bx                  ; BL = byte, BH = count of ones
        mov ah, 8
.bit:
        WAITLO STAT_CLK, [t_bit], .frame
        shr bl, 1                   ; LSB first
        test al, STAT_DATA          ; data is valid while CLK is low
        jz .zero
        or bl, 80h
        inc bh
.zero:
        WAITHI STAT_CLK, [t_bit], .frame
        dec ah
        jnz .bit
        ; parity bit
        WAITLO STAT_CLK, [t_bit], .frame
        test al, STAT_DATA
        jz .p0
        inc bh
.p0:
        WAITHI STAT_CLK, [t_bit], .frame
        ; stop bit
        WAITLO STAT_CLK, [t_bit], .frame
        mov ah, al
        WAITHI STAT_CLK, [t_bit], .frame
        test ah, STAT_DATA          ; stop bit must be 1
        jz .frame
        test bh, 1                  ; odd parity: total ones must be odd
        jz .parity
        mov al, bl
        pop bx
        clc
        ret
.frame:
        mov ah, 2
        jmp .fail
.parity:
        mov ah, 3
.fail:
        pop bx
        stc
        ret

; ---------------------------------------------------------------------------
; ps2_send: send one byte to the device (host-to-device frame, waits for the
; device's ack bit, not for its FA reply).
;   in:  AL = byte
;   out: CF=0 ok, CF=1 failed (lines released)
;   Clobbers AX, CX, DX.
; ---------------------------------------------------------------------------
ps2_send:
        push bx
        mov bl, al
        mov bh, 2                   ; bit 9 (BH bit 1) = stop bit = 1
        test al, al                 ; PF=1 when the number of ones is even
        jpo .odd
        or bh, 1                    ; bit 8 = parity, makes the total odd
.odd:
        mov dx, [port_data]
        LINES OUT_CLKLO             ; request to send: CLK low >= 100 us
        inc dx
        DELAY [d_inhibit]
        dec dx
        LINES OUT_BOTHLO            ; DATA low = start bit
        LINES OUT_DATLO             ; release CLK, the device clocks now
        inc dx
        WAITLO STAT_CLK, [t_txstart], .fail
        mov ah, 10                  ; 8 data + parity + stop
.bit:
        ; CLK is low: present the next bit, the device samples it on the
        ; rising edge
        mov al, OUT_IDLE
        test bl, 1
        jnz .one
        mov al, OUT_DATLO
.one:
        dec dx
        xor al, [drive_xor]
        out dx, al
        inc dx
        shr bx, 1
        WAITHI STAT_CLK, [t_bit], .fail
        dec ah
        jz .ack
        WAITLO STAT_CLK, [t_bit], .fail
        jmp .bit
.ack:
        ; DATA is released (stop bit). The device acks by pulling DATA low
        ; for one more clock.
        WAITLO STAT_DATA, [t_bit], .fail
        WAITLO STAT_CLK, [t_bit], .fail
        WAITHI STAT_CLK, [t_bit], .fail
        WAITHI STAT_DATA, [t_bit], .fail
        pop bx
        clc
        ret
.fail:
        mov al, OUT_IDLE
        call ps2_lines
        pop bx
        stc
        ret

; ---------------------------------------------------------------------------
; ps2_recv_hold: ps2_recv, then hold CLK low at once so that a following
; byte waits for us (the device aborts and retransmits it later). Same
; interface as ps2_recv; CLK is left inhibited.
; ---------------------------------------------------------------------------
ps2_recv_hold:
        call ps2_recv
        push ax
        pushf
        mov al, OUT_CLKLO
        call ps2_lines
        popf
        pop ax
        ret

; ---------------------------------------------------------------------------
; ps2_cmd: send a command byte and read the device's reply (FA = ack).
; Retries 3 times on a send failure or an FE (resend) reply. Leaves CLK
; inhibited.
;   in:  AL = byte
;   out: CF=0, AL = reply; CF=1 no reply
;   Clobbers AX, CX, DX.
; ---------------------------------------------------------------------------
ps2_cmd:
        push si
        mov si, 3
.retry:
        push ax
        call ps2_send
        pop ax
        jc .next
        push ax
        mov dx, [port_status]
        SETCNT [t_resp]
        call ps2_recv_hold
        jc .rfail
        cmp al, 0FEh
        je .rfail
        pop dx                      ; drop the saved command byte
        pop si
        clc
        ret
.rfail:
        pop ax
.next:
        dec si
        jnz .retry
        pop si
        stc
        ret
