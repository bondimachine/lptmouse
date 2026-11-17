; INT 33h function 0Ch event handler for the tests, shaped like the one in
; Sierra's SCI0 interpreter: saves everything, CLI, own DS, logs the call,
; then uses a good chunk of stack (SCI calls into its video driver here).
        cpu 8086
        org 0
handler:
        pushf
        push ax
        push bx
        push cx
        push dx
        push si
        push di
        push bp
        push ds
        push es
        cli
        cld
        push ax
        mov ax, cs
        mov ds, ax
        pop ax
        mov bp, bx                  ; button state
        mov bx, [count]
        cmp bx, 64
        jae .full
        shl bx, 1
        shl bx, 1
        shl bx, 1
        shl bx, 1                   ; 16 bytes per entry
        mov [log + bx], ax
        mov [log + bx + 2], bp
        mov [log + bx + 4], cx
        mov [log + bx + 6], dx
        mov [log + bx + 8], si
        mov [log + bx + 10], di
        mov [log + bx + 12], ss
        mov [log + bx + 14], sp
        inc word [count]
.full:  ; burn 160 bytes of stack like a nested driver call would
        mov ax, ss
        mov es, ax
        sub sp, 160
        mov di, sp
        mov cx, 80
        mov ax, 0A55Ah
        rep stosw
        add sp, 160
        mov ah, 2                   ; get shift flags (SCI's keyboard driver)
        int 16h
        pop es
        pop ds
        pop bp
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        pop ax
        popf
        retf
        align 16
count:  dw 0
        align 16
log:
