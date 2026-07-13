#define _GNU_SOURCE
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int cpu_map[16] = {0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15};
static int cpu_map_loaded = 0;

static void load_cpu_map(void) {
    if (cpu_map_loaded) return;
    cpu_map_loaded = 1;
    const char *env = getenv("NUMA_CPU_MAP");
    if (!env) return;
    char buf[256];
    strncpy(buf, env, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';
    char *p = buf;
    int i = 0;
    while (i < 16) {
        cpu_map[i++] = atoi(p);
        p = strchr(p, ',');
        if (!p) break;
        p++;
    }
}

/* Called from C as: set_affinity(myid) */
void set_affinity(int thread_id)
{
    load_cpu_map();
    if (thread_id < 0 || thread_id >= 16) return;

    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(cpu_map[thread_id], &cpuset);

    if (sched_setaffinity(0, sizeof(cpu_set_t), &cpuset) != 0)
        fprintf(stderr, "[NUMA] sched_setaffinity failed: thread %d -> cpu %d\n",
                thread_id, cpu_map[thread_id]);
}
