; ps2data.inc - variables for ps2lpt.inc / ps2init.inc. Include it in the
; segment DS points to.

%include "bits.inc"

; ---------------------------------------------------------------------------
; Timeouts in polling loop iterations (see calibrate_timeouts)
; ---------------------------------------------------------------------------
t_bit       dw 300      ; edge inside a byte (half clock period <= 50 us)
t_start     dw 3000     ; first clock of a byte after releasing CLK
t_gap       dw 2000     ; next byte of the same packet
t_resp      dw 25000    ; reply to a command (device: up to 20 ms)
t_txstart   dw 20000    ; device starts clocking a host byte (up to 15 ms)
t_resend    dw 10000    ; packet after a resend request
d_inhibit   dw 200      ; CLK held low before a send / to abort (>= 100 us)
reads_per_ms dw 1000


; microseconds -> loop iterations, used by calibrate_timeouts
calib_table:
        PTR_T t_bit
        dw 300
        PTR_T t_start
        dw 3000
        PTR_T t_gap
        dw 2000
        PTR_T t_resp
        dw 25000
        PTR_T t_txstart
        dw 20000
        PTR_T t_resend
        dw 10000
        PTR_T d_inhibit
        dw 200
        PTR_T 0
CALIB_STEP equ PTR_SIZE + 2
