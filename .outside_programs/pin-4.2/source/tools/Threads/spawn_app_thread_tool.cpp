/*
 * Copyright (C) 2010-2025 Intel Corporation.
 * SPDX-License-Identifier: MIT
 */

/* @file
 * This tool check one aspect of PIN_SpawnApplicationThread() which is to test it can exit gracefully.
 * @note PIN_SpawnApplicationThread() is not able today to really create a fully functional application thread
 *       since we don't create an application TLS for it which mean it cannot access TLS (using $fs:0 for example)
 *       which is used by the standard GLIBC in different functions. 
 *       Therefore the function being used in the spawned application thread is very simple that doesn't use libc.
 */

#include "pin.H"

#include <iostream>
#include <fstream>
#include <unistd.h>
#include <sched.h>




static KNOB< BOOL > KnobVerbose(KNOB_MODE_WRITEONCE, "pintool", "v", "0", "Output verbose information");

static ADDRINT threadFunction = 0;

// MAXTHREADS includes the static thread that starts the world, so must be >=2 to test anything!
#define MAXTHREADS 2
#define STACKSIZE 8192

static VOID ThreadCreateCallback(THREADID tid, CONTEXT* ctxt, INT32 flags, VOID* v)
{
    if (KnobVerbose) std::cerr << "Thread create callback for " << tid << "\n";
}

static VOID ThreadExitCallback(THREADID tid, const CONTEXT* ctxt, INT32 code, VOID* v)
{
    std::cerr << "Thread " << tid << " terminated (" << code << ")\n";
    if (ctxt)
    {
        std::cerr << "Thread " << tid << " IP: " << std::hex << PIN_GetContextReg(ctxt, REG_INST_PTR) << std::dec << "\n";
    }
}

static VOID ContextChangeCallback(THREADID tid, CONTEXT_CHANGE_REASON reason, const CONTEXT* from, CONTEXT* to, INT32 info,
                                  VOID* v)
{
    std::cerr << "Thread " << tid << " context change " << reason << " info " << info << "\n";
    if (from)
    {
        std::cerr << "Thread " << tid << " IP: " << std::hex << PIN_GetContextReg(from, REG_INST_PTR) << std::dec << "\n";
    }
}

VOID BeforAppMain(CONTEXT* ctxt, THREADID threadId)
{
    //TraceFile << name << "(" << size << ")" << std::endl;
    ADDRINT* stack = (ADDRINT*)mmap(0, STACKSIZE * sizeof(ADDRINT), PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    ASSERTX(MAP_FAILED != stack);

    if (!threadFunction)
    {
        std::cerr << "Cannot find 'doNothing()' in application\n";
        PIN_ExitProcess(-1);
    }

    CONTEXT newThreadContext;
    PIN_SaveContext(ctxt, &newThreadContext);

    // Fill in sensible values for critical registers.
    PIN_SetContextReg(&newThreadContext, REG_STACK_PTR, ADDRINT(&stack[STACKSIZE-1]));
    PIN_SetContextReg(&newThreadContext, REG_INST_PTR, threadFunction);
    PIN_SetContextReg(&newThreadContext, REG_GFLAGS, 0);

    if (!PIN_SpawnApplicationThread(&newThreadContext))
    {
        std::cerr << "PIN_SpawnApplicationThread failed\n";
        PIN_ExitProcess(-1);
    }
    if (KnobVerbose) std::cerr << "Spawned a new thread (" << threadId << ")\n";
}

static VOID ImageLoadCallback(IMG img, VOID* v)
{
    if (IMG_IsMainExecutable(img))
    {
        ASSERTX((threadFunction == 0));

        if (KnobVerbose) std::cerr << "Looking for doNothing in " << IMG_Name(img) << "\n";

        RTN rtn = RTN_FindByName(img, "doNothing");

        if (RTN_Valid(rtn))
        {
            threadFunction = RTN_Address(rtn);
            if (KnobVerbose) std::cerr << "'doNothing' at " << std::hex << threadFunction << std::dec << "\n";
        }

        rtn = RTN_FindByName(img, "main");
        if (RTN_Valid(rtn))
        {
            RTN_Open(rtn);

            // Instrument malloc() to print the input argument value and the return value.
            RTN_InsertCall(rtn, IPOINT_BEFORE, (AFUNPTR)BeforAppMain, IARG_CONTEXT, IARG_THREAD_ID, IARG_END);

            RTN_Close(rtn);
        }
    }
}

int main(int argc, char* argv[])
{
    if (PIN_Init(argc, argv))
    {
        std::cerr << "Bad arguments\n";
        return -1;
    }

    PIN_InitSymbols();

    IMG_AddInstrumentFunction(ImageLoadCallback, 0);
    PIN_AddThreadStartFunction(ThreadCreateCallback, 0);
    PIN_AddThreadFiniFunction(ThreadExitCallback, 0);
    PIN_AddContextChangeFunction(ContextChangeCallback, 0);

    PIN_StartProgram();
}
