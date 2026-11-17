; LPTMOUSE.VXD - Windows 95 VMOUSE mouse mini-driver for the parallel port
; PS/2 mouse (see README.md for the cable).
;
; This is the native Windows 95 way to add a pointing device (DDK "sermou"
; sample): VMOUSE, Windows 95's own mouse VxD, loads the mini-driver named
; in the device's registry key (MouseDriver=lptmouse.vxd, set by
; LPTMOUSE.INF) and sends it Sys_Dynamic_Device_Init with ECX -> the mouse
; instance. We reset the mouse, virtualize the port IRQ and, on every
; interrupt, read the packet (same code as the DOS driver, interrupts off,
; ring 0) and hand it to VMOUSE with VMD_Post_Pointer_Message. VMOUSE does
; the rest: the standard Windows 95 MOUSE.DRV, acceleration, and INT 33h in
; DOS boxes.
;
; Settings: binary values in the device's driver registry key (written by
; the INF, editable with REGEDIT): Port, IRQ, Rate, Invert (4 bytes each).
;
; Build: nasm -f obj ; wlink format windows vxd dynamic (see makefile)

        bits 32
        cpu 386

%macro VXDCALL 1
        int 20h
        dd %1
%endmacro
Get_System_Time                 equ 0001003Fh
VPICD_Virtualize_IRQ            equ 00030001h
VPICD_Phys_EOI                  equ 00030004h
VPICD_Physically_Unmask         equ 00030009h
VPICD_Force_Default_Behavior    equ 00030011h
VMD_Post_Pointer_Message        equ 000C0003h
VMD_Set_Mouse_Data              equ 000C0006h
CONFIGMG_Read_Registry_Value    equ 0033003Eh

; control messages
System_Exit                     equ 05h
Sys_Critical_Exit               equ 06h
Sys_Dynamic_Device_Init         equ 1Bh
Sys_Dynamic_Device_Exit         equ 1Ch

; Mouse_Instance (VMD.INC)
MI_Flags        equ 00h         ; dw
MI_IO_Base      equ 02h         ; dw
MI_IRQNumber    equ 04h         ; db
MI_PortNum      equ 05h         ; db
MI_MouseType    equ 06h         ; dw
MI_hDevNode     equ 0Ch         ; dd
MI_hIRQ         equ 10h         ; dd
MI_HWINT        equ 14h         ; dd
MI_Disable      equ 1Ch         ; dd
MIF_Detected    equ 0004h
MIF_BadDevNode  equ 0008h
MIF_OTHERMOUSE  equ 0800h

VMD_Type_PS2    equ 4
REG_BINARY      equ 3
CM_REGISTRY_SOFTWARE equ 1

BURST_MAX       equ 9

        segment _LTEXT public align=16 use32 class=CODE

; ===========================================================================
; Device descriptor block (Windows 95 layout), exported as ordinal 1
; ===========================================================================
        global LPTM95_DDB
LPTM95_DDB:
        dd 0                    ; DDB_Next
        dw 0400h                ; DDB_SDK_Version (4.0)
        dw 0                    ; DDB_Req_Device_Number: undefined
        db 1, 0                 ; version 1.0
        dw 0                    ; DDB_Flags
        db 'LPTM95  '           ; DDB_Name
        dd 80000000h            ; DDB_Init_Order: undefined
        dd control_proc         ; DDB_Control_Proc
        dd 0                    ; DDB_V86_API_Proc
        dd 0                    ; DDB_PM_API_Proc
        dd 0                    ; DDB_V86_API_CSIP
        dd 0                    ; DDB_PM_API_CSIP
        dd 0                    ; DDB_Reference_Data
        dd 0                    ; DDB_Service_Table_Ptr
        dd 0                    ; DDB_Service_Table_Size
        dd 0                    ; DDB_Win32_Service_Table
        db 'verP'               ; DDB_Prev  (MASM's DD 'Prev' byte order)
        dd DDB_SIZE             ; DDB_Size
        db '1vsR', '2vsR', '3vsR' ; DDB_Reserved1-3
DDB_SIZE equ $ - LPTM95_DDB

; ===========================================================================
; Data
; ===========================================================================
        align 4
irq_desc:                       ; VPICD_IRQ_Descriptor
        dw 7                    ; VID_IRQ_Number
        dw 0                    ; VID_Options
        dd hw_int_proc          ; VID_Hw_Int_Proc
        dd 0                    ; VID_Virt_Int_Proc
        dd 0                    ; VID_EOI_Proc
        dd 0                    ; VID_Mask_Change_Proc
        dd 0                    ; VID_IRET_Proc
        dd 500                  ; VID_IRET_Time_Out
        dd 0                    ; VID_Hw_Int_Ref

instance        dd 0
irq_handle      dd 0
running         db 0
have_saved      db 0
saved_data      db 0FFh
saved_ctrl      db 0Ch

port_data       dw 0
port_status     dw 0
port_ctrl       dw 0
irq             db 7
drive_xor       db 0
ctrl_val        db 04h
sample_rate     db 40

nbytes          dw 0
burst_buf       times BURST_MAX db 0
cnt_errors      dw 0
cnt_resend      dw 0
resend_empty    db 1            ; always interrupt driven

reg_value       dd 0
reg_len         dd 0
str_port        db 'Port', 0
str_irq         db 'IRQ', 0
str_rate        db 'Rate', 0
str_invert      db 'Invert', 0
valid_rates     db 10, 20, 40, 60, 80, 100, 200, 0

        align 4
%define NO_TRACE
%include "ps2lpt.inc"
%include "ps2data.inc"
%include "burst.inc"
%include "ps2init.inc"
%include "vxdplat.inc"

; ===========================================================================
; Control procedure: EAX = message, EBX = VM handle
; ===========================================================================
control_proc:
        cmp eax, Sys_Dynamic_Device_Init
        je mouse_initialize
        cmp eax, System_Exit
        je .exit
        cmp eax, Sys_Critical_Exit
        je .exit
        clc
        ret
.exit:  call hw_stop
        call restore_port
        clc
        ret

; ---------------------------------------------------------------------------
; Sys_Dynamic_Device_Init from VMOUSE: ECX -> Mouse_Instance (0: none).
; CF=0 if the mouse is there and running.
; ---------------------------------------------------------------------------
mouse_initialize:
        push ebx
        or ecx, ecx
        jz .fail
        mov [instance], ecx
        or word [ecx + MI_Flags], MIF_BadDevNode   ; until we find it
        call read_settings
        mov ecx, [instance]
        mov al, [ecx + MI_IRQNumber]               ; IRQ from the devnode
        cmp al, 2
        jb .irq_ok
        cmp al, 15
        ja .irq_ok
        mov [irq], al
.irq_ok:
        movzx eax, byte [irq]
        mov [irq_desc], ax
        mov ax, [port_data]
        inc ax
        mov [port_status], ax
        inc ax
        mov [port_ctrl], ax
        call save_port
        mov dx, [port_ctrl]                        ; outputs on, IRQ off
        mov al, [ctrl_val]
        out dx, al
        call calibrate_timeouts
        call line_test
        jc .fail_port
        call mouse_init                            ; leaves CLK held low
        jc .fail_port
        mov edi, irq_desc
        VXDCALL VPICD_Virtualize_IRQ
        jc .fail_port
        mov [irq_handle], eax
        VXDCALL VPICD_Physically_Unmask
        ; tell VMOUSE what we have
        mov ecx, [instance]
        mov ax, [port_data]
        mov [ecx + MI_IO_Base], ax
        mov al, [irq]
        mov [ecx + MI_IRQNumber], al
        mov eax, [irq_handle]
        mov [ecx + MI_hIRQ], eax
        mov dword [ecx + MI_HWINT], hw_int_proc
        mov dword [ecx + MI_Disable], disable_proc
        mov ah, VMD_Type_PS2
        mov al, [irq]
        xor ebx, ebx
        mov dh, 3                                  ; buttons
        xor dl, dl                                 ; port number (serial)
        VXDCALL VMD_Set_Mouse_Data
        mov ecx, [instance]
        and word [ecx + MI_Flags], ~MIF_BadDevNode
        or word [ecx + MI_Flags], MIF_Detected | MIF_OTHERMOUSE
        call hw_resume
        pop ebx
        clc
        ret
.fail_port:
        call restore_port
.fail:  pop ebx
        stc
        ret

; ---------------------------------------------------------------------------
; Settings from the driver registry key; keeps the defaults when missing.
; ---------------------------------------------------------------------------
read_settings:
        mov word [port_data], 378h
        mov byte [irq], 7
        mov edi, str_port
        call read_reg
        jc .irq
        cmp eax, 100h
        jb .irq
        cmp eax, 0FFF8h
        ja .irq
        mov [port_data], ax
.irq:   cmp word [port_data], 278h
        jne .irq2
        mov byte [irq], 5
.irq2:  mov edi, str_irq
        call read_reg
        jc .rate
        cmp eax, 2
        jb .rate
        cmp eax, 15
        ja .rate
        mov [irq], al
.rate:  mov edi, str_rate
        call read_reg
        jc .inv
        cmp eax, 255
        ja .inv
        mov esi, valid_rates
.rl:    mov ah, [esi]
        or ah, ah
        jz .inv
        cmp ah, al
        je .rok
        inc esi
        jmp .rl
.rok:   mov [sample_rate], al
.inv:   mov edi, str_invert
        call read_reg
        jc .done
        or eax, eax
        jz .done
        mov byte [drive_xor], 3
.done:  ret

; EDI -> value name. -> CF=0, EAX = value (up to 4 bytes); CF=1 missing
read_reg:
        xor eax, eax
        mov [reg_value], eax
        mov dword [reg_len], 4
        push dword CM_REGISTRY_SOFTWARE
        push dword reg_len
        push dword reg_value
        push dword REG_BINARY
        push edi
        push dword 0                ; no subkey
        mov eax, [instance]
        push dword [eax + MI_hDevNode]
        VXDCALL CONFIGMG_Read_Registry_Value
        add esp, 7 * 4
        or eax, eax                 ; CR_SUCCESS
        jnz .miss
        mov eax, [reg_value]
        clc
        ret
.miss:  stc
        ret

; ===========================================================================
; Hardware interrupt (VPICD): EAX = IRQ handle. Interrupts are disabled.
; ===========================================================================
hw_int_proc:
        pushad
        cmp byte [running], 0
        je .eoi
        mov dx, [port_ctrl]
        mov al, [ctrl_val]          ; port IRQ off while we toggle CLK
        out dx, al
        call read_burst
        mov dx, [port_ctrl]
        mov al, [ctrl_val]
        or al, 10h
        out dx, al
.eoi:   mov eax, [irq_handle]
        VXDCALL VPICD_Phys_EOI
        cmp byte [running], 0
        je .done
        call post_burst
.done:  popad
        clc
        ret

; Validate the burst and post its packets to VMOUSE.
post_burst:
        movzx eax, word [nbytes]
        or eax, eax
        jz .ret
        mov bl, 3
        div bl
        or ah, ah
        jnz .ret                    ; partial burst: drop
        movzx ecx, al
        mov ebx, burst_buf
.pk:    test byte [ebx], 08h        ; bit 3 is always set in the first byte
        jz .ret
        push ebx
        push ecx
        mov dl, [ebx]               ; flags
        movzx esi, byte [ebx + 1]   ; dx (9-bit, sign in the flags)
        test dl, 10h
        jz .xp
        or esi, 0FFFFFF00h
.xp:    movzx edi, byte [ebx + 2]   ; dy
        test dl, 20h
        jz .yp
        or edi, 0FFFFFF00h
.yp:    neg edi                     ; PS/2 Y grows upwards, VMOUSE downwards
        test dl, 0C0h               ; overflow: ignore the movement
        jz .nov
        xor esi, esi
        xor edi, edi
.nov:   xor al, al                  ; AL = 0 0 L R M 0 0 0
        test dl, 1
        jz .nl
        or al, 20h
.nl:    test dl, 2
        jz .nr
        or al, 10h
.nr:    test dl, 4
        jz .nm
        or al, 08h
.nm:    VXDCALL VMD_Post_Pointer_Message
        pop ecx
        pop ebx
        add ebx, 3
        loop .pk
.ret:   ret

; ===========================================================================
; VMOUSE disables the mouse: ESI -> Mouse_Instance
; ===========================================================================
disable_proc:
        call hw_stop
        xor eax, eax
        xchg eax, [esi + MI_hIRQ]
        or eax, eax
        jz .r
        VXDCALL VPICD_Force_Default_Behavior
.r:     call restore_port
        ret

; port IRQ off, CLK held low
hw_stop:
        mov byte [running], 0
        cmp word [port_data], 0
        je .r
        pushfd
        cli
        mov dx, [port_ctrl]
        mov al, [ctrl_val]
        out dx, al
        mov al, OUT_CLKLO
        call ps2_lines
        popfd
.r:     ret

; CLK released, port IRQ on
hw_resume:
        pushfd
        cli
        mov al, OUT_IDLE
        call ps2_lines
        mov dx, [port_ctrl]
        mov al, [ctrl_val]
        or al, 10h
        out dx, al
        mov byte [running], 1
        popfd
        ret

save_port:
        cmp byte [have_saved], 0
        jne .r
        mov dx, [port_data]
        in al, dx
        mov [saved_data], al
        add dx, 2
        in al, dx
        mov [saved_ctrl], al
        mov byte [have_saved], 1
.r:     ret

; give the port back as we found it
restore_port:
        cmp byte [have_saved], 0
        je .r
        pushfd
        cli
        mov dx, [port_data]
        mov al, [saved_data]
        out dx, al
        add dx, 2
        mov al, [saved_ctrl]
        and al, 0DFh                ; never leave the data pins as inputs
        out dx, al
        popfd
.r:     ret
