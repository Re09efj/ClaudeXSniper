/*
 * Copyright (C) 2014-2021 Intel Corporation.
 * SPDX-License-Identifier: MIT
 */

#include <sstream>
#include <cstdlib>

#if defined(TARGET_LINUX)
#include <unistd.h>
#include <sys/types.h>
#endif //TARGET_LINUX

/*
 * This application prints its memory map to stdout
 */

int main()
{
    std::ostringstream os;

#if defined(TARGET_LINUX)
    os << "/bin/cat /proc/" << getpid() << "/maps";
#endif
    system(os.str().c_str());

    return 0;
}
