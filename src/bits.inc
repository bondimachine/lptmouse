; bits.inc - register / pointer helpers for 16 and 32-bit builds.
%ifndef BITS_INC
%define BITS_INC

; Bit width: the same source assembles as 16-bit (DOS, Windows driver) or
; 32-bit flat (the VxD). Pointer registers and the LOOP counter differ.
%if __BITS__ == 32
  %define RSI esi
  %define RDI edi
  %define PTR_T dd
  PTR_SIZE equ 4
  %macro SETCNT 1                   ; LOOP counter = 16-bit value %1
        xor ecx, ecx
        mov cx, %1
  %endmacro
%else
  %define RSI si
  %define RDI di
  %define PTR_T dw
  PTR_SIZE equ 2
  %macro SETCNT 1
        mov cx, %1
  %endmacro
%endif

%endif
