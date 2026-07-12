# Pin

Copyright (C) 2004-2026 Intel Corporation.  
SPDX-License-Identifier: MIT

## Overview

Pin is a tool for the instrumentation of programs. It supports the Linux(R) and Windows operating
systems and executables for the IA-32 and Intel(R) 64-bit architectures.

For license information, see the `intel-simplified-software-license.txt` file and the `licensing`
directory.

For information on how to use Pin, read the manual in `doc/html/index.html`.

For questions and bug reports, please visit <https://groups.io/g/pinheads>.

For downloading Intel(R) X86 Encoder Decoder, please visit <https://github.com/intelxed>.

## System Requirements

### CPU

- Pin is supported on systems with Intel processors. Incompatible or proprietary instructions in
  non-Intel processors may cause Pin to function incorrectly. Any attempt to instrument code not
  supported by Intel processors may lead to failures.

### Linux

- Pin is supported on Linux distros with Linux kernel version >= 4.12.14.
- Building Pin Tools on Linux is supported from GCC 6.1 up to GCC 14. GCC 10 or higher is
  recommended, due to limitations in earlier GCC versions.

### Windows

- Pin is supported on (Desktop) Windows 10 22H2 or higher, and (Server) Windows Server 2022 or
  higher.
- Building Pin Tools on Windows requires `clang-cl` compiler versions 15 or 16.
  - Use of Microsoft CL Compiler is not supported due to LLVM libcxx restrictions.
  - If no Windows headers are used, it may be possible to use a standalone `clang` installation.
    However, we recommend downloading and installing Visual Studio with `clang` as part of the
    Visual Studio installation.
  - Pin only supports Visual Studio 2022 Version 17.6.xx. Newer versions use a newer version of
    `clang`.

## Installation

To install a kit, unpack a downloaded kit and change to the directory.

- For Linux kits, use `tar xzf <downloaded kit file name>` to unpack the kit.
- For Windows kits, use the zip folders feature of Windows or any unzip tool to unpack the kit.

Kit names are of the form:

- `pin-4.<minor>-<build>-g<commit>-<compiler>-<platform>.tar.gz` for Linux kits
- `pin-4.<minor>-<build>-g<commit>-<compiler>-<platform>.zip` for Windows kits

For example:

- `pin-4.0-99625-gc5b279576-gcc-linux.tar.gz`
- `pin-4.0-99625-gc5b279576-clang-windows.zip`

For better security, install in a secure location.

## Example Usage

This example applies to a 64-bit application. For a 32-bit application, use `obj-ia32` instead of
`obj-intel64` and add `TARGET=ia32` to the `make` command.

To build and run a sample tool on Linux:

```bash
cd source/tools/SimpleExamples
make obj-intel64/opcodemix.so
../../../pin -t obj-intel64/opcodemix.so -- /bin/ls
```

This will instrument and run `/bin/ls`. The output for this tool is in `opcodemix.out`.

To build and run a sample tool on Windows, open a command line prompt (depending on the application
type) and run:

```text
cd source\tools\SimpleExamples
make obj-intel64/opcodemix.dll
..\..\..\pin.exe -t obj-intel64\opcodemix.dll -- cmd /C dir
```

This will instrument and run `cmd /C dir`. The output for this tool is in `opcodemix.out`.

Refer to the Examples section in the Pin User Guide for more usage examples.

## Restrictions

- Tools are restricted from linking with any system libraries and/or calling any system calls. See
  the paragraph on PinCRT in the "Additional information for PinTool writers" section for more
  information.
- Pin on Windows requires `msdia140.dll`. This DLL is distributed with the kit.
- There is a known problem of using pin on systems protected by the "McAfee(R) Host Intrusion
  Prevention" antivirus software. See the "Additional Information for Using Pin on Windows"
  section for more information.
- There is a known problem of attaching pin to a running process on Linux systems that prevent the
  use of `ptrace` attach using the `sysctl /proc/sys/kernel/yama/ptrace_scope`. See the
  "Additional information for using Pin on Linux" section for more information.
- Pin performs memory allocations in the application's address space. As a result, memory
  allocations performed by the application can fail. For example, some applications use the
  SmartHeap utility which could perform huge memory allocations.
- There are known problems using Pin with the Google Chrome(TM) browser.
  - On Windows, Pin may fail to attach to a running Chrome process.
  - On Linux, Pin may crash when instrumenting Chrome.
  - A possible workaround is to launch Chrome with the `--no-sandbox` command line switch.
- Pin on Linux can read debug information in formats up to DWARF 5, inclusive.
  - DWARF 6 may work but was not tested.
- Building Pin Tools on Linux only requires linking with both `libdwarf.so` and `libpindwarf.so`,
  provided with Pin Kit.
- There are known issues when instrumenting CET-enabled applications with Pin.
  - Pin disables CET for instrumented process in Launch mode.
  - Instrumenting CET-enabled applications in Attach mode might end in a random crash.
- On Windows, the total combined size of the command line and environment variables block must be
  less than ~60KB for `-follow-execve` to work correctly. If `-follow-execve` is not used, then
  Pin will not explicitly limit the size of these blocks, but they may be limited by the OS.
- On Linux, the size of the command line must be less than ~60KB for `-follow-execve` to work
  correctly. If `-follow-execve` is not used, then Pin will not explicitly limit the size of the
  command line, but it may be limited by the OS.
- On Linux, Pin does not support parsing of compressed debug information.

## Additional Information for PinTool Writers

- Pin is built and distributed with its own OS-agnostic, compiler-agnostic runtime, named Pin RT.
  Pin RT exposes three layers of generic APIs which practically eliminate Pin's and the tools'
  dependency on the host system:
  1. A generic operating system interface, supplying basic OS services such as process control and
     thread control. This layer is not accessible to PinTool developers.
  2. A Musl-based C runtime layer supplying a standard C implementation.
  3. A modern up-to-date C++ runtime based on LLVM's libcxx, supporting C++17 on both Linux and
     Windows. Please note that the current version does not support C++ RTTI, and C++ exceptions
     are supported only on Linux.

  Tools are obliged to use, and link with, Pin RT instead of any system runtime. Tools must
  refrain from using any native system calls, and use Pin RT APIs for any needed functionality.
  This limitation can be alleviated by employing Pin's Remote Procedure Calls (RPCs). For further
  details, consult the "Executing Remote Procedures" section in the Pin User Guide.
- Pin RT does not support the Boost C++ libraries due to lack of C++ RTTI.
- Due to a compatibility issue between operating systems, pin does *not* provide support for
  registering `atexit` functions inside pintools, which means that the behavior of a pintool that
  does so is undefined. If you need such functionality, register a `Fini` function instead. In
  probe mode, the `Fini` function may or may not be called.
- Some APIs from Pin 3 have been deprecated and replaced by Pin RT API. Usage of deprecated APIs
  in Pin Tools will trigger a compilation warning. You can `#define PIN_DEPRECATED_WARNINGS=0` to
  disable these warnings. Please refer to the "Breaking changes between Pin 3 and Pin 4" section
  further down this document. Also note that such deprecated APIs may be completely removed in
  future versions of Pin without prior notice.
- The default `malloc` implementation used by Pin is not async-signal-safe. Correctly written
  tools should avoid allocating or freeing memory using `malloc`, `free`, and similar APIs in
  signal handlers (Linux; see `man 7 signal-safety`) or exception handlers (Windows). However,
  some legacy tools may rely on Pin 3.x `malloc` behavior, which was async-signal-safe. Even Pin's
  internal signal handling is not async safe under some use-cases.
  - If you encounter deadlocks or memory corruption related to memory allocation or freeing inside
    signal or exception handlers, it is possible to add the following knobs to Pin's invocation:
    `-xyzzy -async-safe-malloc`. Passing this knob will use the old Pin 3.x implementation.

## Additional Information for Using Pin on Windows

### General Issues

- Pin provides transparent support for exceptions in the application, but prohibits using
  exceptions in the tool under Windows. If you need to assert some condition, use the `ASSERT()`
  macro defined by pin instead of the standard `assert()`.
- The Image API does not work for GCC-compiled applications.
- There is a known problem of using pin on systems protected by the "McAfee(R) Host Intrusion
  Prevention" antivirus software. We did not test coexistence of pin with other antivirus products
  that perform run-time execution monitoring.
- Pin may not instrument applications that restrict loading of DLLs from non-local drives if Pin
  and or pintool binaries are located on a network drive. To work around this problem, install all
  Pin and Pin tool binaries on a local drive.
- Multi-byte characters support: using the UTF-8 code page, for process arguments and environment
  of Pin binaries is available starting from Windows build 18362, which corresponds to Windows 10
  Version 1903. This covers Windows 10, Windows 11, Windows Server 2022 and Windows Server 2025.

### PinADX Support

- Pin Advanced Debugging Extensions (PinADX) supports Visual Studio 2022.

## Additional Information for Using Pin on Linux

### General Issues

- There is a known problem of attaching Pin to a running process on Linux systems that prevent the
  use of `ptrace` attach using the `sysctl /proc/sys/kernel/yama/ptrace_scope`. There is no
  problem in launching an application with Pin with this limitation. To resolve this, set the
  `kernel/yama/ptrace_scope` `sysctl` to `0`.

---

# Recent Changes

## Changes Added in Pin 4.2

- This version introduces the following enhancements:
  - LIP (Low Integrity Processes) support on Windows:
    - Pin can now instrument low integrity processes on Windows. This includes both launching and
      attaching to low integrity processes.
  - Improved and unified debug symbol processing for Windows and Linux:
    - Added `IMG_AddPreDebugInfoProcessCallback` API to allow tools to control debug symbol
      processing on a per-image basis. Returning `FALSE` from the callback skips debug symbol
      processing for that image.
    - Linux now supports controlling symbol modes via `PIN_InitSymbolsAlt`, similar to Windows.
      See `PIN_InitSymbolsAlt` documentation.
    - See breaking changes below.
  - API enhancement: `PIN_GetSyscallNumber()` now available in syscall exit callbacks:
    - The `PIN_GetSyscallNumber()` API function can now be used in both syscall entry and syscall
      exit callbacks. Previously, this function was only available for use in syscall entry
      callbacks.
    - This enhancement enables tools to retrieve the syscall number when handling syscall exit
      events, simplifying syscall tracking and analysis workflows.
  - Improved performance of `fork` and `execve` on Linux in probe mode:
    - To take full advantage of this improvement, Pin should be called with the `-probe` knob.
  - Improved performance of attach flows on Linux.
  - Lift command line and environment block size limitations on both Windows and Linux:
    - Pin no longer imposes limitations on the size of command line arguments and environment
      variable blocks.
    - On Windows, the total combined size of the command line and environment variables block must
      be less than ~60KB for `-follow-execve` to work correctly. If `-follow-execve` is not used,
      then Pin will not explicitly limit the size of these blocks, but they may be limited by the
      OS.
    - On Linux, the size of the command line must be less than ~60KB for `-follow-execve` to work
      correctly. If `-follow-execve` is not used, then Pin will not explicitly limit the size of
      the command line, but it may be limited by the OS.
- Notable bugs fixed in this version:
  - Pin incorrectly restores `r11` register after syscall.
  - Pin fails to instrument certain Windows applications due to bad `SYSCALL` mapping.
  - Several issues related to running Pin inside containers or on containerized applications on
    Linux.
  - Deadlock inside an application using `waitpid` to wait for all children (`pid == -1`).
  - Deadlock during detach and reattach flows on Linux.
  - Failure to inject when pin is called from a different drive from where Pin is installed on
    Windows.
  - Failure to inject under certain conditions on Windows in Azure DevOps environment.
  - Pin fails to instrument `regedit.exe` on Windows.
  - Pin hangs when instrumenting 3DMark with `-follow-execve` on Windows.
- Breaking changes in this version:
  - Windows: Pin now processes both debug symbols and export symbols when debug info is available.
    Previously, export symbols were only processed when debug symbols were unavailable. The
    previous behavior of `DEBUG_OR_EXPORT_SYMBOLS` is now deprecated. Export symbols are always
    processed. See below.
    - Exported symbols pointing to ILT, Incremental Link Table, thunks are now detected and
      renamed with `@ILT` suffix to avoid conflicts with debug info symbols when debug symbols are
      present. See `PIN_InitSymbolsAlt` documentation. This change should not affect correctness of
      existing Pintools.
  - Linux: Pin now creates new symbols from debug information for functions that do not have
    corresponding symbols in the symbol tables. Previously, debug info was only used to merge or
    adjust existing symbols.
  - `DEBUG_SYMBOLS` is treated as `DEBUG_AND_EXPORT_SYMBOLS` on all supported platforms. This
    means the export and symbol-table symbols are always processed when debug symbol processing is
    requested.
  - The `SYMBOL_INFO_MODE` enum retains the `DEBUG_OR_EXPORT_SYMBOLS` entry. However, it is a
    synonym for `DEBUG_AND_EXPORT_SYMBOLS`. `DEBUG_AND_EXPORT_SYMBOLS` is now the default for
    `PIN_InitSymbols()` on both Windows and Linux. New implementations should not use the
    `DEBUG_OR_EXPORT_SYMBOLS` enum, as it does not work as the name implies.
  - This version removes the `-pin_image_memory_range` knob that was non-functional starting with
    Pin 4.0. Starting with Pin 4.2, the `-pin_memory_range` knob is also used to specify where Pin
    is loaded as well as the memory ranges allowed for Pin's internal use. Note that this knob is
    currently supported only on Linux and is not supported on Windows.
  - The behavior of the `-pin_memory_range` knob is changed compared with Pin 3.x. Starting with
    this version, Pin will attempt to use memory ranges specified by the `-pin_memory_range` knob
    for loading Pin and for its internal memory allocations. However, if these ranges are not
    available, Pin will still use other memory ranges for its operation instead of failing. This
    change allows Pin to operate correctly in more environments, but it also means that the
    `-pin_memory_range` knob is now a hint rather than a strict requirement. Note that this knob
    is currently supported only on Linux and is not supported on Windows.

## Changes Added in Pin 4.1

- This version introduces the following enhancements:
  - Reduce memory consumption on Windows.
  - Add initial support for images mapped using large pages on Windows.
  - Add support for `PIN_SpawnApplicationThread` on Windows.
  - Add new APIs `PIN_GetWindowsSyscallFromKey` and `PIN_GetKeyFromWindowsSyscall`.
  - Add syscall emulation for `__NR_execveat` on Linux.
  - Improve `umask` isolation on Linux.
- Notable bugs fixed in this version:
  - Pin decodes APX instructions although APX is not supported yet.
    - If APX decoding is for any reason desirable, it should be enabled within a callback
      registered using `PIN_AddXedDecodeCallbackFunction`.
  - Pin launcher erroneously parses the application's `-h` knob.
  - Deadlock when application forks with certain Pin logging enabled.
  - Pin is unable to instrument a shell script without a shebang on Linux.

## Changes Added in Pin 4.0

- This version of Pin 4.x is feature-aligned with Pin 3.32. See breaking changes in the section
  below.
- Pin 4.0 highlights include:
  - Pin 3.x compatibility.
  - Full support for C++17, including `filesystem`, threads, and `atomic`.
  - Modern, OS-agnostic, isolated runtime layer.
  - Efficient, POSIX-compliant memory and thread management.
  - Cross-platform compatibility.
  - Support of out-of-process workload offloading.
- Pin launcher (`PIN_KIT/pin`) changes:
  - Linux: `PIN_KIT/pin` is now a 64-bit executable instead of 32-bit, and can run both 64-bit
    and 32-bit applications as before.
  - Linux: `PIN_KIT/pin32` was added in case Pin needs to run on systems without 64-bit support.
  - Launcher sources were removed from the kit sources.
  - Users who utilize the Pin launcher from an unmodified kit will continue to use it as they have
    until now. Unmodified here means the Pin kit hierarchy, including the location of Pin launcher
    and its dependent binaries relative to it, has not changed.
  - Users who move Pin launcher and its dependent libraries in a way that breaks the original kit
    hierarchy, meaning dependent binaries are no longer relative to Pin launcher in the same way
    they were in the original kit, can use new Pin launcher command-line arguments such as
    `-pin_ld_path_64`, `-pin_lib_64`, and others to specify paths of binaries and library folders
    that Pin launcher needs in order to execute correctly.
  - Users can write their own launcher that will run Pin launcher together with these command-line
    arguments.
  - For more information, run `PIN_KIT/pin -help`.
  - Some usage examples can be found under `<pinkit>/source/tools/Launcher`.
- Added new API functions `PIN_CheckReadAccessEx` and `PIN_CheckWriteAccessEx`.

## Known Issues (Most Recent Version)

- The following knobs are not supported:
  - `-debug_info_max_size`
  - `-memrestrict`
  - `-memlimit`
  - `-pin_memory_range` on Windows
  - `-restrict_memory`
- Windows: stack overflow for **internal** threads is not handled properly and cannot be
  intercepted by the Pintool. Pin will terminate with a segmentation fault.
- Debugging an instrumented application using PinADX may fail or hang on exit on Windows.
- Instrumented application remains stuck after Pin exited. This happens rarely and is not expected
  to affect normal use-cases.

## Breaking Changes Between Pin 3 and Pin 4

Pin 4.x introduces a suite of substantial infrastructure enhancements over its predecessor, Pin 3.

To fully understand the scope and implications of these changes, it is strongly advised to read
the Pin User Guide, with particular attention to the sections highlighted in "What's New in Pin
4.x".

Below is a summary of the critical breaking changes when transitioning from Pin 3 to Pin 4:

- The OS-API provided in Pin 3 is no longer supported in Pin 4. Code previously utilizing the
  OS-API should transition to the corresponding Pin RT API, which is compatible with both Windows
  and Linux platforms.
- The `InstLib` folder provided various utilities that were used by some tools adapted from SDE
  and Pinplay. Since Pinplay has long been part of SDE, these utilities and tools are removed from
  Pin. These utilities may be found as part of the SDE kit.
- Pin 4.x shifts to a Musl-based C runtime for improved ISO C and POSIX compliance, departing from
  Pin 3's Bionic-based runtime. Pintools, which previously relied on Bionic's specific behaviors,
  may need to revise their code to align with the more standardized runtime environment. While Pin
  4's C runtime strives for conformity with ISO C and POSIX standards, it does have its own set of
  deviations. These are outlined in the "Pin C Runtime Deviations From Standard Documentation"
  section of the Pin User Guide.
- The following threading-related behaviors have changed in Pin 4:
  1. `OS_THREAD_ID` is no longer synonymous with `NATIVE_TID`, the native system thread
     identifier. Furthermore, internal thread ID representations such as `OS_THREAD_ID` and
     `THREADID` are now subject to cycling and cannot be assumed to remain unique throughout the
     application's execution. For a more in-depth explanation, consult the "Understanding Thread
     Ids in Pin" section of the Pin User Guide.
  2. The functions `PIN_ThreadUid`, `PIN_Yield`, and `PIN_Sleep` are now deprecated and should no
     longer be used. Refer to their respective sections in the user documentation for recommended
     alternatives.
  3. The function `PIN_SpawnInternalThread` has been made more flexible and can now be invoked
     from any point within the code. Additionally, as of Pin 4.0, internal threads can also be
     created using `pthread_create()` and `std::thread()`. For more information, review the
     documentation for `PIN_SpawnInternalThread`.
- The following changes are Linux-specific:
  4. The Pin launcher code is no longer included. Users who need to utilize the Pin launcher
     independently of the Pin kit must now provide additional input parameters. Detailed
     information on this change is provided in the "Recent Changes" section above.
- The following changes are Windows-specific:
  5. Due to constraints associated with LLVM libcxx, Pintools can no longer be compiled using the
     Microsoft MSVC Compiler. Refer to the System Requirements section for more details.
  6. Direct inclusion of `Windows.h` should be replaced with `<windows/pinrt_windows.h>` to
     prevent type conflicts. For guidance on resolving these conflicts, see the "Conflicts between
     Pin and Windows" section in the Pin User Guide.
  7. Pin 4.x changes the way multi-byte, Unicode, characters are handled on Windows. See the
     "Additional information for using Pin on Windows" section above for more details.
- Pin knobs are not parsed nor activated in case of missing application or missing pid to attach
  to. Pin knobs will be activated only if `-- <app>` or `--pid <pid>` was specified.

---

## Disclaimer and Legal Information

The information in this document is subject to change without notice and
Intel Corporation assumes no responsibility or liability for any
errors or inaccuracies that may appear in this document or any
software that may be provided in association with this document. This
document and the software described in it are furnished under license
and may only be used or copied in accordance with the terms of the
license. No license, express or implied, by estoppel or otherwise, to
any intellectual property rights is granted by this document. The
information in this document is provided in connection with Intel
products and should not be construed as a commitment by Intel
Corporation.

EXCEPT AS PROVIDED IN INTEL'S TERMS AND CONDITIONS OF SALE FOR SUCH
PRODUCTS, INTEL ASSUMES NO LIABILITY WHATSOEVER, AND INTEL DISCLAIMS
ANY EXPRESS OR IMPLIED WARRANTY, RELATING TO SALE AND/OR USE OF INTEL
PRODUCTS INCLUDING LIABILITY OR WARRANTIES RELATING TO FITNESS FOR A
PARTICULAR PURPOSE, MERCHANTABILITY, OR INFRINGEMENT OF ANY PATENT,
COPYRIGHT OR OTHER INTELLECTUAL PROPERTY RIGHT. Intel products are not
intended for use in medical, life saving, life sustaining, critical
control or safety systems, or in nuclear facility applications.

Designers must not rely on the absence or characteristics of any
features or instructions marked "reserved" or "undefined." Intel
reserves these for future definition and shall have no responsibility
whatsoever for conflicts or incompatibilities arising from future
changes to them.

The software described in this document may contain software defects
which may cause the product to deviate from published
specifications. Current characterized software defects are available
on request.

Intel and Pentium are trademarks or registered trademarks of
Intel Corporation or its subsidiaries in the U.S. and other countries.

Intel, Xeon, and Intel Xeon Phi are trademarks of Intel Corporation in
the U.S. and/or other countries.

Google Chrome(TM) browser is a trademark of Google LLC. in the U.S. and other countries.

Linux(R) is the registered trademark of Linus Torvalds in the U.S. and other countries.

McAffer(R) is a registered trademark of McAfee, LLC in the U.S. and other countries.

Microsoft, Windows, and the Windows logo are trademarks, or registered trademarks
of Microsoft Corporation in the United States and/or other countries.

*Other names and brands may be claimed as the property of others.

Copyright 2004-2026 Intel Corporation.

Intel Corporation, 2200 Mission College Blvd., Santa Clara, CA 95052-8119, USA.