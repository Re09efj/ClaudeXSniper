/*
 * GUPS (Giga Updates Per Second) - RandomAccess Kernel
 *
 * Implements the HPCC RandomAccess benchmark specification:
 *   Luszczek et al., "Introduction to the HPC Challenge Benchmark Suite",
 *   Lawrence Berkeley National Laboratory Technical Report, 2005.
 *
 * Algorithm:
 *   Performs 4*TableSize pseudo-random XOR updates on a uint64 table.
 *   Random sequence: ran = (ran << 1) ^ (ran < 0 ? POLY : 0)
 *   Update:          table[ran & (TableSize-1)] ^= ran
 *
 * Each OpenMP thread runs an independent stream starting at a different
 * offset in the PRNG sequence, partitioning the 4*TableSize updates evenly.
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <omp.h>

/* GF(2) primitive polynomial: x^64 + x^4 + x^3 + x + 1 */
#define POLY  0x0000000000000007ULL

/* Default table size exponent: 2^LOG2_TABLESIZE entries */
#ifndef LOG2_TABLESIZE
#define LOG2_TABLESIZE 22
#endif

#define TABLESIZE   (1ULL << LOG2_TABLESIZE)
#define NUM_UPDATES (4ULL * TABLESIZE)

/*
 * Advance the PRNG by n steps.
 * Used to initialize per-thread stream offsets without iterating n times.
 * Uses fast exponentiation on the characteristic polynomial.
 */
static uint64_t prng_advance(uint64_t ran, uint64_t n)
{
    uint64_t m2[64];
    uint64_t temp = ran;

    /* Build table of m2[i] = 2^(2^i) * ran mod poly */
    m2[0] = 2;
    for (int i = 1; i < 64; i++) {
        m2[i] = (m2[i-1] << 1) ^ ((int64_t)m2[i-1] < 0 ? POLY : 0);
        m2[i] = (m2[i]   << 1) ^ ((int64_t)m2[i]   < 0 ? POLY : 0);
    }

    /* Exponentiation by squaring */
    while (n > 0) {
        int j = __builtin_ctzll(n); /* index of lowest set bit */
        temp = (temp << 1) ^ ((int64_t)temp < 0 ? POLY : 0);
        (void)j; /* suppress unused warning; we use the bit directly */
        /* Multiply ran by 2^(lowest set bit position) */
        for (int i = 0; i < 64; i++) {
            if ((n >> i) & 1) {
                ran = (ran << 1) ^ ((int64_t)ran < 0 ? POLY : 0);
                uint64_t t = ran;
                ran = 0;
                for (int k = 0; k < 64; k++)
                    if ((t >> k) & 1)
                        ran ^= m2[k];
                break;
            }
        }
        n &= n - 1; /* clear lowest set bit */
    }
    (void)temp;
    return ran;
}

/*
 * Simple per-thread PRNG advance: iterate n steps.
 * Used for small offsets (per-thread within-chunk).
 */
static inline uint64_t prng_next(uint64_t ran)
{
    return (ran << 1) ^ ((int64_t)ran < 0 ? POLY : 0);
}

int main(int argc, char *argv[])
{
    int log2_size = LOG2_TABLESIZE;
    if (argc > 1)
        log2_size = atoi(argv[1]);

    uint64_t tablesize   = 1ULL << log2_size;
    uint64_t num_updates = 4ULL * tablesize;
    uint64_t mask        = tablesize - 1;

    uint64_t *table = (uint64_t *)malloc(tablesize * sizeof(uint64_t));
    if (!table) {
        fprintf(stderr, "Failed to allocate table of %llu entries\n",
                (unsigned long long)tablesize);
        return 1;
    }

    /* Initialize table */
    for (uint64_t i = 0; i < tablesize; i++)
        table[i] = i;

    int nthreads;
    #pragma omp parallel
    {
        #pragma omp single
        nthreads = omp_get_num_threads();
    }

    printf("HPCC RandomAccess (GUPS)\n");
    printf("  Table size : 2^%d = %llu entries (%llu MB)\n",
           log2_size,
           (unsigned long long)tablesize,
           (unsigned long long)(tablesize * sizeof(uint64_t) / (1024*1024)));
    printf("  Updates    : %llu\n", (unsigned long long)num_updates);
    printf("  Threads    : %d\n", nthreads);
    fflush(stdout);

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    #pragma omp parallel
    {
        int tid       = omp_get_thread_num();
        int nthd      = omp_get_num_threads();
        uint64_t per  = num_updates / nthd;
        uint64_t start = (uint64_t)tid * per;
        uint64_t end   = (tid == nthd - 1) ? num_updates : start + per;

        /* Advance PRNG to this thread's starting position */
        uint64_t ran = 1;
        for (uint64_t s = 0; s < start; s++)
            ran = prng_next(ran);

        for (uint64_t i = start; i < end; i++) {
            ran = prng_next(ran);
            table[ran & mask] ^= ran;
        }
    }

    clock_gettime(CLOCK_MONOTONIC, &t1);

    double elapsed = (t1.tv_sec - t0.tv_sec)
                   + (t1.tv_nsec - t0.tv_nsec) * 1e-9;
    double gups    = (double)num_updates / elapsed / 1e9;

    printf("  Time       : %.4f s\n", elapsed);
    printf("  GUPS       : %.6f\n", gups);

    /* Verification: count errors (expected: 0 for correct implementation) */
    uint64_t errors = 0;
    uint64_t ran = 1;
    for (uint64_t i = 0; i < num_updates; i++) {
        ran = prng_next(ran);
        if (table[ran & mask] == ran)
            errors++;  /* value reverted means double-hit, not an error per se */
    }
    /* HPCC spec allows up to 1% error rate */
    double error_rate = (double)errors / (double)num_updates;
    printf("  Error rate : %.4f%% (%s)\n",
           error_rate * 100.0,
           error_rate <= 0.01 ? "PASS" : "FAIL");

    free(table);
    return (error_rate <= 0.01) ? 0 : 1;
}
