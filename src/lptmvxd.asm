; LPTMOUSE.386 - Windows 3.1 / 95 virtual device (VxD) for the parallel port
; PS/2 mouse, used by LPTMOUSE.DRV in 386 enhanced mode.
;
; Why a VxD: in enhanced mode Windows traps every access to the LPT ports
; from ring 3 (the printer VxD virtualizes them) and CLI there only clears a
; virtual flag, so the bit-banging cannot keep up with the mouse clock. The
; VxD runs at ring 0 where port I/O is direct and CLI is real.
;
; How it works: the VxD virtualizes the port's IRQ. On the hardware
; interrupt it reads the packet (same code as the DOS driver, interrupts
; off), queues it and raises the virtual IRQ in the System VM. LPTMOUSE.DRV's
; interrupt handler there fetches the packets through the VxD's protected
; mode API and hands them to Windows.
;
; PM API (INT 2Fh AX=1684h BX=LPTMOUSE_ID gives the entry point):
;   AX=0  version              -> AX = 0100h
;   AX=1  start: BX = port, CL = IRQ, CH = drive xor (0 or 3), DL = rate
;         -> CF=0 ok; CF=1, AX = error (see err_* below)
;   AX=2  get packet           -> CF=1 none; CF=0 BL = flags, CL = dx,
;                                  DL = dy (raw PS/2 bytes), AX = packets left
;   AX=3  stop  (CLK held low, port IRQ off)
;   AX=4  resume
;   AX=5  counters             -> BX packets, CX frame errors, DX resends,
;                                  SI dropped bursts, DI reads per ms
;
; Build: nasm -f obj lptmvxd.asm ; wlink format windows vxd (see makefile)

        bits 32
        cpu 386

LPTMOUSE_ID     equ 3A7Dh       ; VxD device ID (not registered with MS)

; --- VMM / VPICD services (Windows 3.1 DDK VMM.INC / VPICD.INC) ---
%macro VXDCALL 1
        int 20h
        dd %1
%endmacro
Get_Sys_VM_Handle               equ 00010003h
Get_System_Time                 equ 0001003Fh
VPICD_Virtualize_IRQ            equ 00030001h
VPICD_Set_Int_Request           equ 00030002h
VPICD_Clear_Int_Request         equ 00030003h
VPICD_Phys_EOI                  equ 00030004h

; control messages
System_Exit                     equ 5
Sys_Critical_Exit               equ 6

; Client_Reg_Struc offsets
Client_EBX      equ 10h
Client_EDX      equ 14h
Client_ECX      equ 18h
Client_EAX      equ 1Ch
Client_EFlags   equ 2Ch
Client_ESI      equ 04h
Client_EDI      equ 00h
CF_MASK         equ 1

; API errors
err_line        equ 10h         ; + line_test code (1-5)
err_init        equ 20h         ; + mouse_init stage (1-4)
err_virt        equ 30h         ; could not virtualize the IRQ
err_irq         equ 31h         ; bad IRQ number

BURST_MAX       equ 9
RING_SIZE       equ 32          ; packets, power of two

        segment _LTEXT public align=16 use32 class=CODE

; ===========================================================================
; Device descriptor block, exported as ordinal 1
; ===========================================================================
        global LPTMOUSE_DDB
LPTMOUSE_DDB:
        dd 0                    ; DDB_Next
        dw 030Ah                ; DDB_SDK_Version (3.10)
        dw LPTMOUSE_ID          ; DDB_Req_Device_Number
        db 1, 0                 ; version 1.0
        dw 0                    ; DDB_Flags
        db 'LPTMOUSE'           ; DDB_Name (8 chars)
        dd 80000000h            ; DDB_Init_Order: undefined
        dd control_proc         ; DDB_Control_Proc
        dd 0                    ; DDB_V86_API_Proc
        dd pm_api_proc          ; DDB_PM_API_Proc
        dd 0                    ; DDB_V86_API_CSIP
        dd 0                    ; DDB_PM_API_CSIP
        dd 0                    ; DDB_Reference_Data
        dd 0                    ; DDB_Service_Table_Ptr
        dd 0                    ; DDB_Service_Table_Size

; ===========================================================================
; Data
; ===========================================================================
        align 4
irq_desc:                       ; VPICD_IRQ_Descriptor
        dw 7                    ; VID_IRQ_Number
        dw 0                    ; VID_Options
        dd hw_int_proc          ; VID_Hw_Int_Proc
        dd 0                    ; VID_Virt_Int_Proc
        dd eoi_proc             ; VID_EOI_Proc
        dd 0                    ; VID_Mask_Change_Proc
        dd 0                    ; VID_IRET_Proc
        dd 500                  ; VID_IRET_Time_Out

irq_handle      dd 0
sys_vm          dd 0
virtualized     db 0
running         db 0
have_saved      db 0
saved_data      db 0FFh         ; port state before we took it
saved_ctrl      db 0Ch

port_data       dw 0
port_status     dw 0
port_ctrl       dw 0
drive_xor       db 0
ctrl_val        db 04h
sample_rate     db 40

nbytes          dw 0
burst_buf       times BURST_MAX db 0
cnt_packets     dw 0
cnt_errors      dw 0
cnt_resend      dw 0
resend_empty    db 1            ; always interrupt driven
cnt_sync        dw 0

ring_head       dd 0
ring_tail       dd 0
ring            times RING_SIZE * 4 db 0

        align 4
%define NO_TRACE
%include "ps2lpt.inc"
%include "ps2data.inc"
%include "burst.inc"
%include "ps2init.inc"

; ===========================================================================
; Control procedure: EAX = message, EBX = VM handle
; ===========================================================================
control_proc:
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

; give the port back as we found it (e.g. to LPTMOUSE.COM after Windows)
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

; ===========================================================================
; Hardware interrupt (VPICD): EAX = IRQ handle, EBX = current VM.
; Interrupts are disabled.
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
        or al, 10h                  ; port IRQ back on
        out dx, al
        call queue_burst
.eoi:   mov eax, [irq_handle]
        VXDCALL VPICD_Phys_EOI
        call request_if_pending
        popad
        clc
        ret

; The System VM ended its interrupt handler (virtual EOI).
eoi_proc:
        pushad
        mov eax, [irq_handle]
        mov ebx, [sys_vm]
        VXDCALL VPICD_Clear_Int_Request
        call request_if_pending     ; more packets came in meanwhile
        popad
        clc
        ret

request_if_pending:
        mov eax, [ring_head]
        cmp eax, [ring_tail]
        je .r
        mov eax, [irq_handle]
        mov ebx, [sys_vm]
        VXDCALL VPICD_Set_Int_Request
.r:     ret

; Validate the burst and queue its packets.
queue_burst:
        movzx eax, word [nbytes]
        or eax, eax
        jz .ret
        mov bl, 3
        div bl
        or ah, ah
        jnz .sync
        movzx ecx, al
        mov esi, burst_buf
.pk:    test byte [esi], 08h        ; bit 3 is always set in the first byte
        jz .sync
        mov eax, [ring_head]
        mov edx, eax
        inc edx
        and edx, RING_SIZE - 1
        cmp edx, [ring_tail]
        je .next                    ; full: drop
        mov ebx, [esi]              ; 3 packet bytes (+1 ignored)
        mov [ring + eax * 4], ebx
        mov [ring_head], edx
        inc word [cnt_packets]
.next:  add esi, 3
        loop .pk
.ret:   ret
.sync:  inc word [cnt_sync]
        ret

; port IRQ off, CLK held low: the mouse stays quiet
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

%include "vxdplat.inc"

; ===========================================================================
; PM API: EBX = VM handle, EBP -> client registers
; ===========================================================================
pm_api_proc:
        movzx eax, word [ebp + Client_EAX]
        cmp eax, 0
        je .version
        cmp eax, 1
        je .start
        cmp eax, 2
        je .get
        cmp eax, 3
        je .stop
        cmp eax, 4
        je .resume
        cmp eax, 5
        je .counters
.fail:  or dword [ebp + Client_EFlags], CF_MASK
        ret
.ok:    and dword [ebp + Client_EFlags], ~CF_MASK
        ret

.version:
        mov word [ebp + Client_EAX], 0100h
        jmp .ok

.get:   pushfd
        cli
        mov eax, [ring_tail]
        cmp eax, [ring_head]
        je .empty
        mov ecx, [ring + eax * 4]
        inc eax
        and eax, RING_SIZE - 1
        mov [ring_tail], eax
        mov [ebp + Client_EBX], cl  ; flags
        mov [ebp + Client_ECX], ch  ; dx
        shr ecx, 16
        mov [ebp + Client_EDX], cl  ; dy
        mov eax, [ring_head]
        sub eax, [ring_tail]
        and eax, RING_SIZE - 1
        mov [ebp + Client_EAX], ax
        popfd
        jmp .ok
.empty: popfd
        jmp .fail

.stop:  call hw_stop
        jmp .ok

.resume:
        cmp byte [virtualized], 0
        je .fail
        call hw_resume
        jmp .ok

.counters:
        mov ax, [cnt_packets]
        mov [ebp + Client_EBX], ax
        mov ax, [cnt_errors]
        mov [ebp + Client_ECX], ax
        mov ax, [cnt_resend]
        mov [ebp + Client_EDX], ax
        mov ax, [cnt_sync]
        mov [ebp + Client_ESI], ax
        mov ax, [reads_per_ms]
        mov [ebp + Client_EDI], ax
        jmp .ok

.start: call api_start
        mov [ebp + Client_EAX], ax
        jc .fail
        jmp .ok

; start the mouse: client BX = port, CL = IRQ, CH = xor, DL = rate
; -> CF, AX = 0 or error
api_start:
        call hw_stop
        mov ax, [ebp + Client_EBX]
        mov [port_data], ax
        inc ax
        mov [port_status], ax
        inc ax
        mov [port_ctrl], ax
        mov al, [ebp + Client_ECX + 1]
        mov [drive_xor], al
        mov al, [ebp + Client_EDX]
        mov [sample_rate], al
        movzx eax, byte [ebp + Client_ECX]
        cmp byte [virtualized], 0
        jne .virt_done
        cmp eax, 2
        jb .bad_irq
        cmp eax, 15
        ja .bad_irq
        mov [irq_desc], ax
.virt_done:
        cmp byte [have_saved], 0
        jne .saved
        mov dx, [port_data]
        in al, dx
        mov [saved_data], al
        add dx, 2
        in al, dx
        mov [saved_ctrl], al
        mov byte [have_saved], 1
.saved: mov dx, [port_ctrl]         ; outputs on, port IRQ off
        mov al, [ctrl_val]
        out dx, al
        call calibrate_timeouts
        call line_test
        jc .line
        call mouse_init
        jc .init
        cmp byte [virtualized], 0
        jne .go
        VXDCALL Get_Sys_VM_Handle
        mov [sys_vm], ebx
        mov edi, irq_desc
        VXDCALL VPICD_Virtualize_IRQ
        jc .virt
        mov [irq_handle], eax
        mov byte [virtualized], 1
.go:    xor eax, eax
        mov [ring_head], eax
        mov [ring_tail], eax
        call hw_resume
        xor ax, ax
        clc
        ret
.line:  movzx ax, al
        add ax, err_line
        stc
        ret
.init:  movzx ax, al
        add ax, err_init
        stc
        ret
.virt:  mov ax, err_virt
        stc
        ret
.bad_irq:
        mov ax, err_irq
        stc
        ret
