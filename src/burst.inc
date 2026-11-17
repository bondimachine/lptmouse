; burst.inc - read one packet burst from the mouse (16 or 32-bit).
; Needs ps2lpt.inc. The includer defines:
;   burst_buf (BURST_MAX bytes), nbytes (dw), cnt_errors (dw), cnt_resend (dw),
;   resend_empty (byte: nonzero when an interrupt means a packet was sent)
; Must run with interrupts disabled.

; Abort whatever the mouse is sending, then read it again from the start.
read_burst:
        mov al, OUT_CLKLO
        call ps2_lines
        mov dx, [port_status]
        DELAY [d_inhibit]
        mov al, OUT_IDLE
        call ps2_lines
        SETCNT [t_start]
        call read_bytes
        ; A partial packet means the interrupt came so late that the first
        ; byte(s) had already been sent: ask the mouse to resend the packet.
        ; Nothing at all means the interrupt came after the whole packet
        ; (something kept interrupts off for longer than a packet takes):
        ; when interrupt driven, ask for it again too. The mouse only keeps
        ; its last packet, so earlier ones missed the same way stay lost.
        mov ax, [nbytes]
        or ax, ax
        jnz .got
        cmp byte [resend_empty], 0
        je .ret
        jmp .resend
.got:   mov bl, 3
        div bl
        or ah, ah
        jz .ret
.resend:
        inc word [cnt_resend]
        mov al, 0FEh
        call ps2_send               ; leaves the lines released
        mov word [nbytes], 0
        jc .ret
        SETCNT [t_resend]
        call read_bytes
        cmp byte [burst_buf], 0FAh  ; some mice ack the resend first
        jne .ret
        cmp word [nbytes], 4
        jne .ret
        mov al, [burst_buf + 1]     ; drop the FA
        mov [burst_buf], al
        mov al, [burst_buf + 2]
        mov [burst_buf + 1], al
        mov al, [burst_buf + 3]
        mov [burst_buf + 2], al
        mov word [nbytes], 3
.ret:   ret

; Read bytes until the mouse stops sending. CX = wait for the first byte.
; CLK must be released. -> burst_buf, nbytes (0 after a bad frame)
read_bytes:
        mov dx, [port_status]
        mov RDI, burst_buf
.next:  call ps2_recv
        jc .end
        mov [RDI], al
        inc RDI
        cmp RDI, burst_buf + BURST_MAX
        jae .done
        SETCNT [t_gap]
        jmp .next
.end:   cmp ah, 1                   ; 1 = nothing more to read
        je .done
        inc word [cnt_errors]       ; bad frame: drop the whole burst
        mov RDI, burst_buf
.done:  sub RDI, burst_buf
        mov [nbytes], di
        ret

