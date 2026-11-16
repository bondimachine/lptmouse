; ps2init.inc - mouse reset / setup, cable test and timeout calibration.
; Assembles as 16-bit or 32-bit code (see ps2lpt.inc).
;
; The includer defines sample_rate (db) and provides:
;   wait_ticks     wait CX (zero-extended) * 55 ms, interrupts enabled
;   measure_reads  DX:AX = status port reads per 55 ms
; With NO_TRACE defined trace_byte does nothing, otherwise the includer
; defines init_trace (db) and print_hex8 / print_char.

; release CLK, read one byte (long timeout), inhibit again. IF must be 0.
recv_reply:
        mov al, OUT_IDLE
        call ps2_lines
        mov dx, [port_status]
        SETCNT [t_resp]
        jmp ps2_recv_hold

; print the reply byte in AL if tracing (keeps AX and flags)
trace_byte:
%ifdef NO_TRACE
        ret
%else
        pushf
        cmp byte [init_trace], 0
        je .no
        push ax
        call print_hex8
        mov al, ' '
        call print_char
        pop ax
.no:    popf
        ret
%endif

; ---------------------------------------------------------------------------
; mouse_init: reset the mouse, set the sample rate, enable streaming.
; Leaves CLK inhibited (held low) and interrupts enabled.
;   out: CF=0 ok; CF=1, AL = stage that failed:
;        1 no reply to reset, 2 no self-test result, 3 rate rejected,
;        4 enable rejected
; ---------------------------------------------------------------------------
mouse_init:
        cli
        mov al, OUT_IDLE
        call ps2_lines
        mov al, 0FFh                ; reset
        call ps2_cmd
        jc .f1
        call trace_byte
        cmp al, 0FAh
        jne .f1
        ; Hold CLK low while the self test runs (~0.5 s): the mouse keeps its
        ; AA 00 until we release the clock, so we can wait with interrupts on.
        mov al, OUT_CLKLO
        call ps2_lines
        mov cx, 14                  ; ~0.8 s
        call wait_ticks
        cli
        call recv_reply
        jc .f2
        call trace_byte
        cmp al, 0AAh
        jne .f2
        call recv_reply             ; device ID, 00 for a standard mouse
        jc .f2
        call trace_byte

        mov al, 0F3h                ; set sample rate
        call ps2_cmd
        jc .f3
        call trace_byte
        cmp al, 0FAh
        jne .f3
        mov al, [sample_rate]
        call ps2_cmd
        jc .f3
        call trace_byte
        cmp al, 0FAh
        jne .f3

        mov al, 0F4h                ; enable data reporting
        call ps2_cmd
        jc .f4
        call trace_byte
        cmp al, 0FAh
        jne .f4

        mov al, OUT_CLKLO
        call ps2_lines
        sti
        clc
        ret
.f1:    mov al, 1
        jmp .fail
.f2:    mov al, 2
        jmp .fail
.f3:    mov al, 3
        jmp .fail
.f4:    mov al, 4
.fail:  push ax
        mov al, OUT_CLKLO
        call ps2_lines
        pop ax
        sti
        stc
        ret

; ---------------------------------------------------------------------------
; line_test: check the cable without talking to the mouse.
;   out: CF=0 ok; CF=1, AL = 1 CLK stuck low, 2 DATA stuck low,
;        3 D0 cannot pull CLK low, 4 D1 cannot pull DATA low,
;        5 CLK and DATA move together
; DATA is only pulled low while CLK is held low (the mouse ignores the lines
; while inhibited), and released before CLK.
; ---------------------------------------------------------------------------
line_test:
        cli
        mov al, OUT_IDLE
        call ps2_lines
        mov dx, [port_status]
        DELAY 100
        in al, dx
        test al, STAT_CLK
        jz .e1
        test al, STAT_DATA
        jz .e2
        mov al, OUT_CLKLO
        call ps2_lines
        DELAY 20
        in al, dx
        mov ah, al
        test ah, STAT_CLK
        jnz .e3
        test ah, STAT_DATA
        jz .e5
        mov al, OUT_BOTHLO
        call ps2_lines
        DELAY 20
        in al, dx
        mov ah, al
        mov al, OUT_CLKLO
        call ps2_lines
        test ah, STAT_DATA
        jnz .e4
        DELAY 20
        in al, dx                   ; DATA must come back up
        test al, STAT_DATA
        jz .e5
        mov al, OUT_IDLE
        call ps2_lines
        DELAY 100
        sti
        clc
        ret
.e1:    mov al, 1
        jmp .f
.e2:    mov al, 2
        jmp .f
.e3:    mov al, 3
        jmp .f
.e4:    mov al, 4
        jmp .f
.e5:    mov al, 5
.f:     push ax
        mov al, OUT_IDLE
        call ps2_lines
        pop ax
        sti
        stc
        ret

; ---------------------------------------------------------------------------
; calibrate_timeouts: measure the port speed and convert the timeouts from
; microseconds to polling loop iterations (calib_table in ps2data.inc).
; port_status must be set.
; ---------------------------------------------------------------------------
calibrate_timeouts:
        call measure_reads          ; DX:AX = reads per 55 ms tick
        mov bx, 55
        cmp dx, bx
        jb .div
        mov ax, 0FFFFh
        jmp .set
.div:   div bx
.set:   or ax, ax
        jnz .rpm
        inc ax
.rpm:   mov [reads_per_ms], ax
        mov RSI, calib_table
.l:     mov RDI, [RSI]
        or RDI, RDI
        jz .done
        mov ax, [RSI + PTR_SIZE]
        mul word [reads_per_ms]
        mov bx, 1000
        cmp dx, bx
        jae .max
        div bx
        cmp ax, 10
        jae .st
        mov ax, 10
        jmp .st
.max:   mov ax, 0FFFFh
.st:    mov [RDI], ax
        add RSI, CALIB_STEP
        jmp .l
.done:  ret
