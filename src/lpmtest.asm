; LPMTEST.COM - bring-up / diagnostic tool for the parallel port PS/2 mouse
; cable. Does not stay resident.
;
;   LPMTEST [/Ln | /Pxxx] [/X] [/W] [/Rnn]
;     /W   only watch the CLK and DATA lines live (no mouse commands)
;
; Steps: measures the port read speed, checks the cable, resets the mouse
; (printing every reply), then shows the packets as you move the mouse.
;
; Build: nasm -f bin -o lpmtest.com lpmtest.asm

        cpu 8086
        org 100h

start:  jmp main

port_data       dw 0
port_status     dw 0
drive_xor       db 0
sample_rate     db 40
init_trace      db 1
opt_port        dw 0
opt_watch       db 0

BURST_MAX       equ 9
burst_buf       times BURST_MAX db 0
nbytes          dw 0
last_err        db 0
cnt_err         dw 0
cnt_pk          dw 0
sum_x           dw 0
sum_y           dw 0

%include "ps2lpt.inc"
%include "ps2data.inc"
%include "util.inc"
%include "dosplat.inc"
%include "ps2init.inc"

msg_banner  db 'LPMTEST - parallel port PS/2 mouse diagnostics', 13, 10, 0
msg_usage   db 'Usage: LPMTEST [/Ln | /Pxxx] [/X] [/W] [/Rnn]', 13, 10
            db '  /W  watch the CLK / DATA lines only', 13, 10, 0
msg_noport  db 'No parallel port found (use /Lx or /Pxxx).', 13, 10, 0
msg_port    db 'Port ', 0
msg_h       db 'h', 13, 10, 0
msg_speed   db 'Status port reads per ms: ', 0
msg_speed2  db ' (timeouts scaled to it)', 13, 10, 0
msg_lines   db 'Cable check: ', 0
msg_ok      db 'ok', 13, 10, 0
msg_reset   db 'Mouse replies (expect FA AA 00 FA FA FA): ', 0
msg_fail    db 13, 10, 'FAILED at stage ', 0
msg_stream  db 13, 10, 'Move the mouse and press buttons. Any key exits.', 13, 10, 0
msg_watch   db 'Watching the lines (any key exits). Lines are released.', 13, 10, 0
msg_clk     db 13, 'CLK=', 0
msg_data    db '  DATA=', 0
msg_err     db '  frame error ', 0
msg_pk      db '  L', 0
msg_sum     db '  total ', 0
msg_errs    db '  errors ', 0

line_errs   dw le1, le2, le3, le4, le5
le1 db 'CLK (pin 10) is low with nothing driving it: mouse not powered, or no pull-up.', 13, 10, 0
le2 db 'DATA (pin 13) is low with nothing driving it: mouse not powered, or no pull-up.', 13, 10, 0
le3 db 'D0 (pin 2) cannot pull CLK low: check the diode and the pin 2 / pin 10 wiring.', 13, 10, 0
le4 db 'D1 (pin 3) cannot pull DATA low: check the diode and the pin 3 / pin 13 wiring.', 13, 10, 0
le5 db 'CLK and DATA move together: the lines are shorted or swapped.', 13, 10, 0

main:
        cld
        mov si, msg_banner
        call print_str
        call parse_args
        jnc .args
        mov si, msg_usage
        jmp fail
.args:  call select_port
        jnc .port
        mov si, msg_noport
        jmp fail
.port:  mov si, msg_port
        call print_str
        mov ax, [port_data]
        call print_hex16
        mov si, msg_h
        call print_str
        ; control register: data outputs enabled, port IRQ off
        mov dx, [port_data]
        add dx, 2
        mov al, 04h
        out dx, al

        mov si, msg_speed
        call print_str
        call calibrate_timeouts
        mov ax, [reads_per_ms]
        call print_dec
        mov si, msg_speed2
        call print_str

        cmp byte [opt_watch], 0
        jne watch

        mov si, msg_lines
        call print_str
        call line_test
        jnc .lok
        call print_crlf
        mov bl, al
        dec bl
        xor bh, bh
        shl bx, 1
        mov si, [line_errs + bx]
        jmp fail
.lok:   mov si, msg_ok
        call print_str

        mov si, msg_reset
        call print_str
        call mouse_init
        jnc .iok
        push ax
        mov si, msg_fail
        call print_str
        pop ax
        xor ah, ah
        call print_dec
        call print_crlf
        mov ax, 4C01h
        int 21h
.iok:
        mov si, msg_stream
        call print_str

stream:
        cli
        call read_burst
        sti
        cmp word [nbytes], 0
        jne .show
        cmp byte [last_err], 0
        je .key
        mov si, msg_err
        call print_str
        mov al, [last_err]
        call print_hex8
        call print_crlf
        jmp .key
.show:  ; raw bytes
        mov si, burst_buf
        mov cx, [nbytes]
.raw:   lodsb
        call print_hex8
        mov al, ' '
        call print_char
        loop .raw
        cmp word [nbytes], 3
        jne .nl
        inc word [cnt_pk]
        call show_packet
.nl:    call print_crlf
.key:   mov ah, 1
        int 16h
        jz stream
        xor ah, ah
        int 16h
        mov al, OUT_CLKLO
        call ps2_lines
        mov ax, 4C00h
        int 21h

; decode burst_buf as one packet
show_packet:
        mov si, msg_pk
        call print_str
        mov bl, [burst_buf]
        mov al, '-'
        test bl, 1
        jz .l
        mov al, 'X'
.l:     call print_char
        mov al, ' '
        call print_char
        mov al, 'M'
        call print_char
        mov al, '-'
        test bl, 4
        jz .m
        mov al, 'X'
.m:     call print_char
        mov al, ' '
        call print_char
        mov al, 'R'
        call print_char
        mov al, '-'
        test bl, 2
        jz .r
        mov al, 'X'
.r:     call print_char
        test bl, 8
        jnz .sync
        mov al, '!'                 ; bit 3 not set: out of sync
        call print_char
.sync:  mov al, ' '
        call print_char
        ; dx
        mov al, [burst_buf + 1]
        xor ah, ah
        test bl, 10h
        jz .xp
        mov ah, 0FFh
.xp:    add [sum_x], ax
        call print_sdec
        mov al, ','
        call print_char
        mov al, [burst_buf + 2]
        xor ah, ah
        test bl, 20h
        jz .yp
        mov ah, 0FFh
.yp:    add [sum_y], ax
        call print_sdec
        test bl, 0C0h
        jz .nov
        mov al, ' '
        call print_char
        mov al, 'O'                 ; overflow
        call print_char
.nov:   mov si, msg_sum
        call print_str
        mov ax, [sum_x]
        call print_sdec
        mov al, ','
        call print_char
        mov ax, [sum_y]
        call print_sdec
        mov si, msg_errs
        call print_str
        mov ax, [cnt_err]
        call print_dec
        ret

; Release CLK, read what the mouse has, hold CLK low again (poll style).
read_burst:
        mov word [nbytes], 0
        mov byte [last_err], 0
        mov al, OUT_IDLE
        call ps2_lines
        mov dx, [port_status]
        mov di, burst_buf
        mov cx, [t_start]
.next:  call ps2_recv
        jc .end
        mov [di], al
        inc di
        cmp di, burst_buf + BURST_MAX
        jae .done
        mov cx, [t_gap]
        jmp .next
.end:   cmp ah, 1
        je .done
        mov [last_err], ah
        inc word [cnt_err]
        mov di, burst_buf
.done:  mov al, OUT_CLKLO
        call ps2_lines
        sub di, burst_buf
        mov [nbytes], di
        ret

; live view of the two lines
watch:
        mov si, msg_watch
        call print_str
        mov al, OUT_IDLE
        call ps2_lines
.l:     mov dx, [port_status]
        in al, dx
        mov bl, al
        mov si, msg_clk
        call print_str
        mov al, '0'
        test bl, STAT_CLK
        jz .c
        mov al, '1'
.c:     call print_char
        mov si, msg_data
        call print_str
        mov al, '0'
        test bl, STAT_DATA
        jz .d
        mov al, '1'
.d:     call print_char
        mov ah, 1
        int 16h
        jz .l
        xor ah, ah
        int 16h
        call print_crlf
        mov ax, 4C00h
        int 21h

fail:   call print_str
        mov ax, 4C01h
        int 21h

valid_rates db 10, 20, 40, 60, 80, 100, 200, 0

parse_args:
        mov si, 81h
.l:     lodsb
        cmp al, 13
        je .end
        cmp al, ' '
        je .l
        cmp al, 9
        je .l
        cmp al, '/'
        je .opt
        cmp al, '-'
        je .opt
        jmp .bad
.opt:   lodsb
        cmp al, 'a'
        jb .up
        cmp al, 'z'
        ja .up
        sub al, 20h
.up:    call parse_port_opt
        jnc .l
        cmp al, 'X'
        je .x
        cmp al, 'W'
        je .w
        cmp al, 'R'
        je .rate
        jmp .bad
.x:     mov byte [drive_xor], 3
        jmp .l
.w:     mov byte [opt_watch], 1
        jmp .l
.rate:  call skip_sep
        call parse_dec
        jc .bad
        or ah, ah
        jnz .bad
        mov bx, valid_rates
.rl:    mov ah, [bx]
        or ah, ah
        jz .bad
        cmp ah, al
        je .rok
        inc bx
        jmp .rl
.rok:   mov [sample_rate], al
        jmp .l
.end:   clc
        ret
.bad:   stc
        ret
