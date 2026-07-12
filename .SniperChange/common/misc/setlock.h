#ifndef SETLOCK_H
#define SETLOCK_H

#include "lock.h"
#include "selock.h"

#include <vector>
#include <pthread.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <linux/futex.h>

/* Cache set lock */

class _SetLock
{
   public:
      _SetLock(UInt32 core_offset, UInt32 num_sharers);
      void acquire_exclusive(void);
      void release_exclusive(void);
      void acquire_shared(UInt32 core_id);
      void release_shared(UInt32 core_id);
      void upgrade(UInt32 core_id);
      void downgrade(UInt32 core_id);

   private:
      // 2026-07-12: pthread_mutex_t requires the same thread to lock/unlock it
      // (unlocking from a different thread is undefined behavior per POSIX).
      // downgrade() is called by the thread that performed acquire_exclusive()
      // (the host pthread of the calling core), but leaves one slot locked;
      // that slot is later unlocked by release_shared() from a *different*
      // core's host pthread. This violates pthread_mutex ownership rules and
      // was confirmed by ThreadSanitizer as "unlock of an unlocked mutex
      // (or by a wrong thread)" during the LU/2TH/W silent-hang investigation
      // (see Documents/SniperBugFix.md).
      //
      // Fix: a minimal futex-based binary lock with no thread-ownership
      // requirement (any thread may release() a lock acquired by another
      // thread, unlike pthread_mutex_t). Deliberately kept as a single
      // trivially-copyable int (not wrapping Semaphore/Lock, which are not
      // safe to bitwise-copy): CacheMasterCntlr::createSetLocks() constructs
      // one SetLock prototype and duplicates it via
      // std::vector<SetLock>::resize(n, prototype), which copy-constructs n
      // instances from that single prototype. A fresh/untouched plain int
      // (value 1 = unlocked) survives this copy safely; a live Semaphore/Lock
      // object (containing internal synchronization primitives) does not.
      class PersetLock
      {
         public:
            PersetLock() : _val(1) {}
            void acquire()
            {
               while (!__sync_bool_compare_and_swap(&_val, 1, 0))
               {
                  syscall(SYS_futex, &_val, FUTEX_WAIT | FUTEX_PRIVATE_FLAG, 0, NULL, NULL, 0);
               }
            }
            void release()
            {
               __sync_lock_test_and_set(&_val, 1);
               syscall(SYS_futex, &_val, FUTEX_WAKE | FUTEX_PRIVATE_FLAG, 1, NULL, NULL, 0);
            }
         private:
            volatile int _val;
      } __attribute__ ((aligned (64)));

      std::vector<PersetLock> m_locks;
      UInt32 m_core_offset;
      #ifdef TIME_LOCKS
      TotalTimer* _timer;
      #endif
};

class _SELock : SELock
{
   public:
      _SELock(UInt32 core_offset, UInt32 num_sharers) : SELock() {}
      void acquire_shared(UInt32 core_id) { SELock::acquire_shared(); }
      void release_shared(UInt32 core_id) { SELock::release_shared(); }
      void downgrade(UInt32 core_id)      { SELock::downgrade(); }
      void upgrade(UInt32 core_id)        { SELock::upgrade(); }
};

#if 0
  typedef SELock SetLock;
#else
  typedef _SetLock SetLock;
#endif

#endif // SETLOCK_H
