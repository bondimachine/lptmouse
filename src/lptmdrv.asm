; LPTMOUSE.DRV - Windows 3.1 / 95 mouse driver for a PS/2 mouse wired to the
; parallel port (see README.md for the cable).
;
; Standard mode: the driver reads the mouse itself, exactly like the DOS
; driver (the port IRQ makes us abort the mouse's transfer and read the
; packet again in a tight loop with interrupts off).
;
; 386 enhanced mode: ring 3 port access is trapped and CLI is virtual there,
; so the work is done by LPTMOUSE.386 (device=lptmouse.386 in [386Enh]).
; It virtualizes the port IRQ, reads the packets at ring 0 and raises the
; virtual IRQ; our handler fetches them through its PM API. Without the VxD
; the driver falls back to reading the port itself, which may drop packets.
;
; Settings, SYSTEM.INI:
;   [LPTMouse]
;   Port=378        port address in hex (default: LPT1 from the BIOS)
;   IRQ=7           port IRQ (default 7, 5 for port 278)
;   Rate=40         sample rate: 10, 20, 40, 60, 80, 100, 200
;   Invert=0        1 = inverting line driver (transistors / 74LS05)
;
; Exports (MOUSE.DRV interface): Inquire @1, Enable @2, Disable @3,
; MouseGetIntVect @4, WEP. Module name MOUSE.
;
; Build: nasm -f obj, wlink format windows dll (see makefile)

        bits 16
        cpu 286                     ; Windows 3.1 needs at least a 286

        segment _TEXT public align=16 use16 class=CODE
        segment _DATA public align=16 use16 class=DATA
        group DGROUP _DATA

        extern GetPrivateProfileString
        extern GetPrivateProfileInt

LPTMOUSE_ID     equ 3A7Dh

MODE_NONE       equ 0
MODE_DIRECT     equ 1           ; we read the port (standard mode)
MODE_VXD        equ 2           ; LPTMOUSE.386 reads it (enhanced mode)

; event_proc status bits (AX)
SF_MOVEMENT     equ 0001h
SF_B1_DOWN      equ 0002h
SF_B1_UP        equ 0004h
SF_B2_DOWN      equ 0008h
SF_B2_UP        equ 0010h
SF_B3_DOWN      equ 0020h
SF_B3_UP        equ 0040h

BURST_MAX       equ 9
MAX_PACKETS     equ 8           ; per interrupt

; ===========================================================================
        segment _DATA

mode            db MODE_NONE
mouse_exists    db 0
enabled         db 0
irq             db 7
vector          db 0FFh         ; interrupt vector, FF = no mouse
ctrl_val        db 04h
pic_was_masked  db 0
sample_rate     db 40
drive_xor       db 0

port_data       dw 0
port_status     dw 0
port_ctrl       dw 0
saved_data      db 0FFh         ; port state before we took it
saved_ctrl      db 0Ch

event_proc      dd 0
old_vector      dd 0
vxd_entry       dd 0

nbytes          dw 0
burst_buf       times BURST_MAX db 0
cnt_errors      dw 0
cnt_resend      dw 0
cnt_packets     dw 0

old_buttons     db 0
vector_tmp      db 0FFh
opt_port        dw 0            ; (used by dosplat.inc helpers)

str_app         db 'LPTMouse', 0
str_port        db 'Port', 0
str_irq         db 'IRQ', 0
str_rate        db 'Rate', 0
str_invert      db 'Invert', 0
str_empty       db 0
str_ini         db 'system.ini', 0
port_str        times 12 db 0

valid_rates     db 10, 20, 40, 60, 80, 100, 200, 0

%include "ps2data.inc"

; ===========================================================================
        segment _TEXT

%define NO_TRACE
%define WIN16
%include "ps2lpt.inc"
%include "burst.inc"
%include "ps2init.inc"
%include "dosplat.inc"

; ---------------------------------------------------------------------------
; LibEntry: DS = DGROUP, DI = instance, CX = heap size. AX <> 0 = loaded.
; Finds the port, resets the mouse and leaves it quiet until Enable.
; ---------------------------------------------------------------------------
..start:
LibEntry:
        push si
        push di
        push es
        call read_settings
        call detect_mode
        cmp byte [mode], MODE_VXD
        je .vxd
        call direct_init
        jmp .done
.vxd:   call vxd_init
.done:  pop es
        pop di
        pop si
        mov ax, 1                   ; always load; Inquire reports no mouse
        retf

; WORD FAR PASCAL WEP(int)
        global WEP
WEP:
        mov ax, 1
        retf 2

; ---------------------------------------------------------------------------
; int FAR PASCAL Inquire(LPMOUSEINFO) -> bytes filled (14)
; ---------------------------------------------------------------------------
        global Inquire
Inquire:
        push bp
        mov bp, sp
        push ds
        push di
        mov ax, DGROUP
        mov ds, ax
        les di, [bp + 6]
        xor ax, ax
        cmp byte [mouse_exists], 0
        je .ne
        dec al                      ; msExists = FF
.ne:    mov ah, 0FFh                ; msRelative
        stosw
        mov ax, 2                   ; msNumButtons
        stosw
        mov al, [sample_rate]       ; msRate
        xor ah, ah
        stosw
        mov ax, 2                   ; msXThreshold
        stosw
        stosw                       ; msYThreshold
        xor ax, ax
        stosw                       ; msXRes
        stosw                       ; msYRes
        mov ax, 14
        pop di
        pop ds
        pop bp
        retf 4

; ---------------------------------------------------------------------------
; void FAR PASCAL Enable(FARPROC event_proc)
; ---------------------------------------------------------------------------
        global Enable
Enable:
        push bp
        mov bp, sp
        push ds
        push si
        push di
        mov ax, DGROUP
        mov ds, ax
        cli
        mov ax, [bp + 6]
        mov [event_proc], ax
        mov ax, [bp + 8]
        mov [event_proc + 2], ax
        sti
        cmp byte [mouse_exists], 0
        je .done
        cmp byte [enabled], 0
        jne .done
        call hook_irq
        cmp byte [mode], MODE_VXD
        jne .direct
        mov ax, 4                   ; VxD: resume
        call far [vxd_entry]
        jmp .unmask
.direct:
        call direct_take_port
.unmask:
        call unmask_irq
        mov byte [enabled], 1
.done:  pop di
        pop si
        pop ds
        pop bp
        retf 4

; ---------------------------------------------------------------------------
; void FAR PASCAL Disable(void)
; ---------------------------------------------------------------------------
        global Disable
Disable:
        push ds
        push si
        push di
        mov ax, DGROUP
        mov ds, ax
        cmp byte [enabled], 0
        je .done
        call mask_irq
        cmp byte [mode], MODE_VXD
        jne .direct
        mov ax, 3                   ; VxD: stop
        call far [vxd_entry]
        jmp .unhook
.direct:
        call direct_release_port
.unhook:
        call unhook_irq
        mov byte [enabled], 0
.done:  pop di
        pop si
        pop ds
        retf

; ---------------------------------------------------------------------------
; int FAR PASCAL MouseGetIntVect(void) -> vector, -1 if no mouse
; ---------------------------------------------------------------------------
        global MouseGetIntVect
MouseGetIntVect:
        push ds
        mov ax, DGROUP
        mov ds, ax
        mov al, [vector]
        cbw
        pop ds
        retf

; ===========================================================================
; Setup
; ===========================================================================

; SYSTEM.INI [LPTMouse] -> port_data, irq, sample_rate, drive_xor
read_settings:
        ; Port (hex string)
        push ds
        push str_app
        push ds
        push str_port
        push ds
        push str_empty
        push ds
        push port_str
        push word 11
        push ds
        push str_ini
        call far GetPrivateProfileString
        mov si, port_str
        call parse_hex
        jnc .have_port
        push es                     ; default: LPT1 from the BIOS data area
        BIOS_ES
        mov ax, [es:BIOS_LPT]
        pop es
.have_port:
        mov [port_data], ax
        mov bx, ax
        inc ax
        mov [port_status], ax
        inc ax
        mov [port_ctrl], ax
        ; IRQ
        mov ax, 7
        cmp bx, 278h
        jne .irqdef
        mov ax, 5
.irqdef:
        push ds
        push str_app
        push ds
        push str_irq
        push ax
        push ds
        push str_ini
        call far GetPrivateProfileInt
        cmp ax, 2
        jb .badirq
        cmp ax, 15
        ja .badirq
        mov [irq], al
.badirq:
        ; Rate
        push ds
        push str_app
        push ds
        push str_rate
        push word 40
        push ds
        push str_ini
        call far GetPrivateProfileInt
        or ah, ah
        jnz .rdone                  ; > 255: not a valid rate, keep 40
        mov si, valid_rates
.rl:    mov ah, [si]
        or ah, ah
        jz .rdone                   ; not in the list: keep 40
        cmp ah, al
        je .rok
        inc si
        jmp .rl
.rok:   mov [sample_rate], al
.rdone:
        ; Invert
        push ds
        push str_app
        push ds
        push str_invert
        push word 0
        push ds
        push str_ini
        call far GetPrivateProfileInt
        or ax, ax
        jz .noinv
        mov byte [drive_xor], 3
.noinv:
        ; interrupt vector of the IRQ
        mov al, [irq]
        cmp al, 8
        jb .master
        add al, 70h - 8
        jmp .vec
.master:
        add al, 8
.vec:   mov [vector_tmp], al
        ret

; enhanced mode with LPTMOUSE.386 loaded -> MODE_VXD, else MODE_DIRECT
detect_mode:
        mov byte [mode], MODE_DIRECT
        mov ax, 1600h
        int 2Fh
        test al, 7Fh                ; 00h / 80h: not enhanced mode
        jz .r
        xor di, di
        mov es, di
        mov ax, 1684h
        mov bx, LPTMOUSE_ID
        int 2Fh
        mov ax, es
        or ax, di
        jz .r
        mov [vxd_entry], di
        mov [vxd_entry + 2], es
        mov byte [mode], MODE_VXD
.r:     ret

vxd_init:
        cmp word [port_data], 0
        je .fail
        mov bx, [port_data]
        mov cl, [irq]
        mov ch, [drive_xor]
        mov dl, [sample_rate]
        mov ax, 1                   ; start
        call far [vxd_entry]
        jc .fail
        mov ax, 3                   ; stop until Enable
        call far [vxd_entry]
        jmp mouse_found
.fail:  ret

direct_init:
        cmp word [port_data], 0
        je .fail
        mov dx, [port_data]         ; remember the port state
        in al, dx
        mov [saved_data], al
        add dx, 2
        in al, dx
        mov [saved_ctrl], al
        mov al, [ctrl_val]          ; outputs on, port IRQ off
        out dx, al
        call calibrate_timeouts
        call line_test
        jc .fail
        call mouse_init             ; leaves CLK held low
        jc .fail
        jmp mouse_found
.fail:  call direct_release_port
        ret

mouse_found:
        mov byte [mouse_exists], 1
        mov al, [vector_tmp]
        mov [vector], al
        mov byte [old_buttons], 0
        ret

; standard mode: CLK released, port IRQ on
direct_take_port:
        cli
        mov al, OUT_IDLE
        call ps2_lines
        mov dx, [port_ctrl]
        mov al, [ctrl_val]
        or al, 10h
        out dx, al
        sti
        ret

; give the port back as we found it (e.g. to LPTMOUSE.COM)
direct_release_port:
        cli
        mov dx, [port_ctrl]
        mov al, [ctrl_val]
        out dx, al                  ; port IRQ off first
        mov dx, [port_data]
        mov al, [saved_data]
        out dx, al
        add dx, 2
        mov al, [saved_ctrl]
        and al, 0DFh                ; never leave the data pins as inputs
        out dx, al
        sti
        ret

hook_irq:
        mov ah, 35h
        mov al, [vector]
        int 21h
        mov [old_vector], bx
        mov [old_vector + 2], es
        push ds
        mov ah, 25h
        mov al, [vector]
        mov dx, mouse_isr
        push cs
        pop ds
        int 21h
        pop ds
        ret

unhook_irq:
        push ds
        mov ah, 25h
        mov al, [vector]
        lds dx, [old_vector]
        int 21h
        pop ds
        ret

; PIC mask port / bit of our IRQ -> DX, AH
pic_bit:
        mov cl, [irq]
        mov dx, 21h
        cmp cl, 8
        jb .m
        sub cl, 8
        mov dx, 0A1h
.m:     mov ah, 1
        shl ah, cl
        ret

unmask_irq:
        call pic_bit
        cli
        in al, dx
        mov byte [pic_was_masked], 0
        test al, ah
        jz .was_open
        mov byte [pic_was_masked], 1
.was_open:
        not ah
        and al, ah
        out dx, al
        cmp byte [irq], 8           ; slave IRQ: make sure IRQ2 is open
        jb .r
        in al, 21h
        and al, 0FBh
        out 21h, al
.r:     sti
        ret

mask_irq:
        cmp byte [pic_was_masked], 0
        je .r
        call pic_bit
        cli
        in al, dx
        or al, ah
        out dx, al
        sti
.r:     ret

; ===========================================================================
; Interrupt handler
; ===========================================================================
mouse_isr:
        push ax
        push bx
        push cx
        push dx
        push si
        push di
        push bp
        push ds
        push es
        mov ax, DGROUP
        mov ds, ax
        mov bp, sp
        sub sp, MAX_PACKETS * 4     ; local copy of the packets
        xor si, si                  ; bytes stored in the local copy

        cmp byte [mode], MODE_VXD
        je .vxd

        ; --- standard mode: read the port ourselves ---
        cmp byte [irq], 7
        jne .real
        mov al, 0Bh                 ; spurious IRQ7? (in-service bit clear)
        out 20h, al
        in al, 20h
        mov ah, al
        mov al, 0Ah
        out 20h, al
        test ah, 80h
        jz .noeoi
.real:  mov dx, [port_ctrl]
        mov al, [ctrl_val]          ; port IRQ off while we toggle CLK
        out dx, al
        call read_burst
        mov dx, [port_ctrl]
        mov al, [ctrl_val]
        or al, 10h
        out dx, al
        ; copy the valid packets
        mov ax, [nbytes]
        or ax, ax
        jz .eoi
        mov bl, 3
        div bl
        or ah, ah
        jnz .eoi                    ; partial burst: drop
        mov cl, al
        xor ch, ch
        mov bx, burst_buf
.cp:    test byte [bx], 08h
        jz .eoi                     ; out of sync: drop the rest
        mov al, [bx]
        mov [ss:bp + si - MAX_PACKETS * 4], al
        mov al, [bx + 1]
        mov [ss:bp + si - MAX_PACKETS * 4 + 1], al
        mov al, [bx + 2]
        mov [ss:bp + si - MAX_PACKETS * 4 + 2], al
        add si, 4
        add bx, 3
        loop .cp
        jmp .eoi

        ; --- enhanced mode: fetch the packets from LPTMOUSE.386 ---
.vxd:   mov ax, 2
        call far [vxd_entry]
        jc .eoi
        mov [ss:bp + si - MAX_PACKETS * 4], bl
        mov [ss:bp + si - MAX_PACKETS * 4 + 1], cl
        mov [ss:bp + si - MAX_PACKETS * 4 + 2], dl
        add si, 4
        cmp si, MAX_PACKETS * 4
        jb .vxd

.eoi:   mov al, 20h
        cmp byte [irq], 8
        jb .m
        out 0A0h, al
.m:     out 20h, al
.noeoi:
        ; hand the packets to Windows, interrupts on
        xor di, di
.ev:    cmp di, si
        jae .out
        mov al, [ss:bp + di - MAX_PACKETS * 4]
        mov bl, [ss:bp + di - MAX_PACKETS * 4 + 1]
        mov cl, [ss:bp + di - MAX_PACKETS * 4 + 2]
        push si
        push di
        call send_event
        pop di
        pop si
        add di, 4
        jmp .ev
.out:   mov sp, bp
        pop es
        pop ds
        pop bp
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        pop ax
        iret

; AL = flags, BL = dx, CL = dy (raw PS/2 packet) -> event_proc
send_event:
        inc word [cnt_packets]
        mov ah, al
        ; deltas
        xor bh, bh
        test ah, 10h
        jz .x
        mov bh, 0FFh
.x:     xor ch, ch
        test ah, 20h
        jz .y
        mov ch, 0FFh
.y:     neg cx                      ; PS/2 Y grows upwards
        test ah, 0C0h               ; overflow: ignore the movement
        jz .nov
        xor bx, bx
        xor cx, cx
.nov:   xor dx, dx                  ; DX = status
        mov si, bx
        or si, cx
        jz .nomove
        mov dl, SF_MOVEMENT
.nomove:
        ; buttons: bit 0 left, 1 right, 2 middle
        and ah, 7
        mov al, ah
        xor al, [old_buttons]       ; changed
        mov [old_buttons], ah
        test al, 1
        jz .b2
        test ah, 1
        mov si, SF_B1_DOWN
        jnz .b1d
        mov si, SF_B1_UP
.b1d:   or dx, si
.b2:    test al, 2
        jz .b3
        test ah, 2
        mov si, SF_B2_DOWN
        jnz .b2d
        mov si, SF_B2_UP
.b2d:   or dx, si
.b3:    test al, 4
        jz .go
        test ah, 4
        mov si, SF_B3_DOWN
        jnz .b3d
        mov si, SF_B3_UP
.b3d:   or dx, si
.go:    or dx, dx
        jz .r                       ; nothing happened
        mov ax, dx
        mov dx, 2                   ; number of buttons
        xor si, si
        xor di, di
        cmp word [event_proc + 2], 0
        je .r
        push ds
        sti
        call far [event_proc]
        cli
        pop ds
.r:     ret
