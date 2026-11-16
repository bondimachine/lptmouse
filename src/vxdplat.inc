; vxdplat.inc - platform pieces for ps2init.inc in a VxD (32-bit, ring 0):
; timing from the VMM system clock. Needs the VXDCALL macro and
; Get_System_Time defined, port_status in the data.

; ===========================================================================

; wait CX * 55 ms, interrupts enabled
wait_ticks:
        pushad
        sti
        movzx ecx, cx
        imul ecx, ecx, 55
        add ecx, 1
        VXDCALL Get_System_Time
        mov ebx, eax
.l:     VXDCALL Get_System_Time
        sub eax, ebx
        cmp eax, ecx
        jb .l
        popad
        ret

; DX:AX = status port reads per 55 ms (measured over >= 110 ms)
measure_reads:
        push ebx
        push ecx
        push esi
        sti
        VXDCALL Get_System_Time
        mov ebx, eax
.align: VXDCALL Get_System_Time     ; start on a clock change
        cmp eax, ebx
        je .align
        mov ebx, eax
        mov dx, [port_status]
        xor esi, esi
.l:     in al, dx
        inc esi
        test esi, 0FFh
        jnz .l
        push edx
        VXDCALL Get_System_Time
        pop edx
        sub eax, ebx
        cmp eax, 110
        jb .l
        mov ecx, eax                ; elapsed ms
        mov eax, esi
        mov edx, 55
        mul edx
        div ecx                     ; reads per 55 ms
        cmp eax, 0FFFFFFh
        jbe .ok
        mov eax, 0FFFFFFh
.ok:    mov edx, eax
        shr edx, 16
        pop esi
        pop ecx
        pop ebx
        ret

