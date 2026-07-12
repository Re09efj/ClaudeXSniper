;
; Copyright (C) 2025-2025 Intel Corporation.
; SPDX-License-Identifier: MIT
;

include asm_macros.inc

PROLOGUE

.code
apx_ins_func PROC
    ; Should create "bt dword ptr [r9], r23d"
    db 0D5h, 0E3h, 0A3h, 039h, 099h, 07Eh, 0C3h, 0B8h, 069h, 02Eh
    ret
apx_ins_func ENDP

end

