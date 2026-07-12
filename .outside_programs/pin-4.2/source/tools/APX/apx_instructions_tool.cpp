/*
 * Copyright (C) 2025-2025 Intel Corporation.
 * SPDX-License-Identifier: MIT
 */

#include "pin.H"
#include <iostream>
#include <fstream>

void AnalyzeMemRead(THREADID threadid, ADDRINT ip, ADDRINT addr, UINT32 size)
{
    printf("."); // So it will not inline this function.
}

void Instruction(INS ins, VOID* v)
{
    if (INS_IsMemoryRead(ins))
    {
        // Instrument the memory read
        INS_InsertPredicatedCall(ins, IPOINT_BEFORE, (AFUNPTR)AnalyzeMemRead, IARG_THREAD_ID, IARG_INST_PTR, IARG_MEMORYREAD_EA,
                                 IARG_MEMORYREAD_SIZE, IARG_END);
    }
}

VOID Fini(INT32 code, VOID* v) { printf("\n"); }

int main(int argc, char* argv[])
{
    // Initialize pin
    if (PIN_Init(argc, argv)) return -1;
    // Register Instruction to be called to instrument instructions
    INS_AddInstrumentFunction(Instruction, 0);

    PIN_AddFiniFunction(Fini, 0);

    PIN_StartProgram();
}