/*
 * Copyright (C) 2025-2025 Intel Corporation.
 * SPDX-License-Identifier: MIT
 */

#include "pin.H"

// enable APX.
void XedSettings(xed_decoded_inst_t* xedd) { xed3_operand_set_no_apx(xedd, 0); }

int main(int argc, char* argv[])
{
    // Initialize pin
    PIN_Init(argc, argv);

    PIN_AddXedDecodeCallbackFunction(XedSettings);

    // Start the program, never returns
    PIN_StartProgram();

    return 0;
}
