#ifndef NUMA_AFFINITY_HPP_
#define NUMA_AFFINITY_HPP_

#define _GNU_SOURCE
#include <sched.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

static int _na_cpu_map[16] = {0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15};
static int _na_loaded = 0;

static void _na_load(void) {
    if (_na_loaded) return;
    _na_loaded = 1;
    const char *env = getenv("NUMA_CPU_MAP");
    if (!env) return;
    char buf[256];
    strncpy(buf, env, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';
    char *p = buf;
    int i = 0;
    while (i < 16) {
        _na_cpu_map[i++] = atoi(p);
        p = strchr(p, ',');
        if (!p) break;
        p++;
    }
}

static inline void numa_set_affinity(int tid) {
    _na_load();
    if (tid < 0 || tid >= 16) return;
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(_na_cpu_map[tid], &cpuset);
    if (sched_setaffinity(0, sizeof(cpu_set_t), &cpuset) != 0)
        fprintf(stderr, "[NUMA] sched_setaffinity failed: thread %d -> cpu %d\n",
                tid, _na_cpu_map[tid]);
}

#endif  // NUMA_AFFINITY_HPP_
