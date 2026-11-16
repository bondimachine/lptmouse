; util.inc - console output helpers (DOS / BIOS), 8086.

; AL = character
print_char:
        push ax
        push dx
        mov dl, al
        mov ah, 02h
        int 21h
        pop dx
        pop ax
        ret

; DS:SI = zero-terminated string
print_str:
        push ax
        push si
.l:     lodsb
        or al, al
        jz .e
        call print_char
        jmp .l
.e:     pop si
        pop ax
        ret

print_crlf:
        push ax
        mov al, 13
        call print_char
        mov al, 10
        call print_char
        pop ax
        ret

; AL = value
print_hex8:
        push ax
        push cx
        mov ah, al
        mov cl, 4
        shr al, cl
        call .nib
        mov al, ah
        and al, 0Fh
        call .nib
        pop cx
        pop ax
        ret
.nib:   add al, '0'
        cmp al, '9'
        jbe .p
        add al, 7
.p:     jmp print_char

; AX = value
print_hex16:
        xchg al, ah
        call print_hex8
        xchg al, ah
        jmp print_hex8

; AX = unsigned value
print_dec:
        push ax
        push bx
        push cx
        push dx
        mov bx, 10
        xor cx, cx
.d:     xor dx, dx
        div bx
        push dx
        inc cx
        or ax, ax
        jnz .d
.o:     pop ax
        add al, '0'
        call print_char
        loop .o
        pop dx
        pop cx
        pop bx
        pop ax
        ret

; AX = signed value
print_sdec:
        push ax
        or ax, ax
        jns .p
        push ax
        mov al, '-'
        call print_char
        pop ax
        neg ax
.p:     call print_dec
        pop ax
        ret
