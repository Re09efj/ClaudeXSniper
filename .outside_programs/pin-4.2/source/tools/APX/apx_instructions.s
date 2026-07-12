/*
 * Copyright (C) 2025-2025 Intel Corporation.
 * SPDX-License-Identifier: MIT
 */

#include "asm_macros.h"

.data

.text

DECLARE_FUNCTION_AS(apx_ins_func)
apx_ins_func:
    // Should create "bt dword ptr [r9], r23d"
    .byte 0xD5, 0xE3, 0xA3, 0x39, 0x99, 0x7E, 0xC3, 0xB8, 0x69, 0x2E
    ret
END_FUNCTION(apx_ins_func)