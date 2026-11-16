; dosplat.inc - real mode DOS (and Windows 16-bit, with WIN16 defined)
; platform pieces: BIOS timer tick, LPT table, command line parsing.
; 16-bit only.

%ifdef WIN16
        extern __0040H              ; KERNEL: selector of the BIOS data area
%macro BIOS_ES 0
        mov ax, __0040H
        mov es, ax
%endmacro
BIOS_TICK   equ 6Ch
BIOS_LPT    equ 08h
%else
%macro BIOS_ES 0
        xor ax, ax
        mov es, ax
%endmacro
BIOS_TICK   equ 46Ch
BIOS_LPT    equ 408h
%endif

; wait CX BIOS timer ticks (55 ms each), interrupts enabled
wait_ticks:
        push ax
        push bx
        push es
        sti
        BIOS_ES
        inc cx                      ; first tick may be partial
        mov bx, [es:BIOS_TICK]
.l:     mov ax, [es:BIOS_TICK]
        sub ax, bx
        cmp ax, cx
        jb .l
        pop es
        pop bx
        pop ax
        ret


; ---------------------------------------------------------------------------
; Measure how long a status port read takes: count reads during one BIOS
; tick. out: DX:AX = reads per tick (~55 ms)
; ---------------------------------------------------------------------------
measure_reads:
        push bx
        push cx
        push si
        push es
        sti
        BIOS_ES
        mov bx, [es:BIOS_TICK]
.w1:    cmp bx, [es:BIOS_TICK]      ; align to a tick edge
        je .w1
        mov bx, [es:BIOS_TICK]
        mov dx, [port_status]
        xor si, si                  ; high word
        xor cx, cx                  ; low word
.l:     in al, dx
        inc cx
        jnz .nc
        inc si
.nc:    cmp bx, [es:BIOS_TICK]
        je .l
        mov ax, cx
        mov dx, si
        pop es
        pop si
        pop cx
        pop bx
        ret

; ---------------------------------------------------------------------------
; Command line parsing helpers. SI points into the command tail.
; ---------------------------------------------------------------------------

; skip an optional ':' or '='
skip_sep:
        mov al, [si]
        cmp al, ':'
        je .s
        cmp al, '='
        jne .r
.s:     inc si
.r:     ret

; parse a decimal number -> AX (CF=1 if no digits)
parse_dec:
        push bx
        push cx
        push dx
        xor bx, bx
        xor cx, cx                  ; digit count
.l:     mov al, [si]
        sub al, '0'
        cmp al, 9
        ja .e
        inc si
        inc cx
        xor ah, ah
        push ax
        mov ax, 10
        mul bx
        pop bx
        add bx, ax
        jmp .l
.e:     mov ax, bx
        cmp cx, 1                   ; CF=1 when CX=0
        pop dx
        pop cx
        pop bx
        ret

; parse a hex number -> AX (CF=1 if no digits)
parse_hex:
        push bx
        push cx
        xor bx, bx
        xor cx, cx
.l:     mov al, [si]
        cmp al, 'a'
        jb .up
        sub al, 20h
.up:    sub al, '0'
        cmp al, 9
        jbe .dig
        sub al, 7                   ; 'A'-'0'-10
        cmp al, 10
        jb .e
        cmp al, 15
        ja .e
.dig:   inc si
        inc cx
        push cx
        mov cl, 4
        shl bx, cl
        pop cx
        or bl, al
        jmp .l
.e:     mov ax, bx
        cmp cx, 1
        pop cx
        pop bx
        ret

; Parse the port options shared by both programs:
;   /L1../L3  use LPTn from the BIOS table
;   /Pxxx     port address in hex
; Called with SI after the option letter, AL = option letter (upper case).
; out: CF=1 if the option was not a port option (SI unchanged)
;      sets [opt_port] (0 = not given)
parse_port_opt:
        cmp al, 'L'
        je .lpt
        cmp al, 'P'
        je .hex
        stc
        ret
.lpt:   call skip_sep
        call parse_dec
        jc .bad
        cmp ax, 1
        jb .bad
        cmp ax, 3
        ja .bad
        dec ax
        shl ax, 1
        push es
        push bx
        mov bx, ax
        push ax
        BIOS_ES
        pop ax
        mov ax, [es:BIOS_LPT + bx]
        pop bx
        pop es
        or ax, ax
        jz .bad
        mov [opt_port], ax
        clc
        ret
.hex:   call skip_sep
        call parse_hex
        jc .bad
        mov [opt_port], ax
        clc
        ret
.bad:   mov word [opt_port], 0FFFFh ; invalid
        clc
        ret

; Pick the port: [opt_port] or LPT1 from the BIOS table. Sets port_data,
; port_status. CF=1 if there is no usable port.
select_port:
        mov ax, [opt_port]
        cmp ax, 0FFFFh
        je .bad
        or ax, ax
        jnz .set
        push es
        BIOS_ES
        mov ax, [es:BIOS_LPT]
        pop es
        or ax, ax
        jz .bad
.set:   mov [port_data], ax
        inc ax
        mov [port_status], ax
        clc
        ret
.bad:   stc
        ret
