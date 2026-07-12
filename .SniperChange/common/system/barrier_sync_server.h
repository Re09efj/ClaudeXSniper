#ifndef __BARRIER_SYNC_SERVER_H__
#define __BARRIER_SYNC_SERVER_H__

#include "fixed_types.h"
#include "cond.h"
#include "hooks_manager.h"

#include <vector>
#include <atomic>

class CoreManager;

class BarrierSyncServer : public ClockSkewMinimizationServer
{
   private:
      SubsecondTime m_barrier_interval;
      SubsecondTime m_next_barrier_time;
      std::vector<SubsecondTime> m_local_clock_list;
      std::vector<bool> m_barrier_acquire_list;
      std::vector<ConditionVariable*> m_core_cond;
      std::vector<core_id_t> m_to_release;
      std::vector<core_id_t> m_core_group;
      std::vector<thread_id_t> m_core_thread;
      SubsecondTime m_global_time;
      bool m_fastforward;
      volatile bool m_disable;

      // 2026-07-12: getGlobalTime() is called cross-thread from many places
      // throughout the codebase (syscall_model.cc, stats.cc, trace_thread.cc,
      // scheduler_pinned_base.cc, etc.), by cores that do *not* necessarily
      // hold ThreadManager's lock -- unlike m_global_time/m_next_barrier_time
      // themselves, which are only ever mutated from within BarrierSyncServer's
      // own methods while that lock *is* held (synchronize(), barrierRelease()
      // via advance()/stallThread()). ThreadSanitizer confirmed a genuine data
      // race on SubsecondTime's underlying m_time field during the LU/2TH/W
      // silent-hang investigation (see Documents/SniperBugFix.md).
      //
      // Rather than making SubsecondTime itself atomic (it's a pervasive,
      // performance-hot value type used almost everywhere in the simulator;
      // atomicizing it wholesale would be invasive and could hurt performance
      // for the overwhelming majority of single-threaded uses that don't need
      // it), keep "mirror" atomic copies of just the two fields that
      // getGlobalTime() exposes cross-thread, updated (in femtoseconds, the
      // unit SubsecondTime stores internally -- see FS_1=1 in
      // subsecond_time.h) immediately after every write to the real fields.
      // All *internal* reads/writes of m_global_time/m_next_barrier_time
      // (comparisons, +=, etc.) are untouched and keep using the plain
      // SubsecondTime fields as before, since those are already protected by
      // ThreadManager's lock; only the externally-exposed accessor changes.
      std::atomic<uint64_t> m_global_time_fs_mirror{0};
      std::atomic<uint64_t> m_next_barrier_time_fs_mirror{0};

      bool isBarrierReached(void);
      bool barrierRelease(thread_id_t thread_id = INVALID_THREAD_ID, bool continue_until_release = false);
      void abortBarrier(void);
      bool isCoreRunning(core_id_t core_id, bool siblings = true);
      void releaseThread(thread_id_t thread_id);
      void signal();
      void doRelease(int n);

      static SInt64 hookThreadExit(UInt64 object, UInt64 argument) {
         ((BarrierSyncServer*)object)->threadExit((HooksManager::ThreadTime*)argument); return 0;
      }
      static SInt64 hookThreadStall(UInt64 object, UInt64 argument) {
         ((BarrierSyncServer*)object)->threadStall((HooksManager::ThreadStall*)argument); return 0;
      }
      static SInt64 hookThreadMigrate(UInt64 object, UInt64 argument) {
         ((BarrierSyncServer*)object)->threadMigrate((HooksManager::ThreadMigrate*)argument); return 0;
      }
      void threadExit(HooksManager::ThreadTime *argument);
      void threadStall(HooksManager::ThreadStall *argument);
      void threadMigrate(HooksManager::ThreadMigrate *argument);

   public:
      BarrierSyncServer();
      ~BarrierSyncServer();

      virtual void setDisable(bool disable);
      virtual void setGroup(core_id_t core_id, core_id_t master_core_id);
      void synchronize(core_id_t core_id, SubsecondTime time);
      void release() { abortBarrier(); }
      void advance();
      void setFastForward(bool fastforward, SubsecondTime next_barrier_time = SubsecondTime::MaxTime());
      SubsecondTime getGlobalTime(bool upper_bound = false) { return m_barrier_interval == SubsecondTime::MaxTime() ? SubsecondTime::FS(m_global_time_fs_mirror.load()) : (upper_bound ? SubsecondTime::FS(m_next_barrier_time_fs_mirror.load()) : SubsecondTime::FS(m_global_time_fs_mirror.load())); }
      void setBarrierInterval(SubsecondTime barrier_interval) { m_barrier_interval = barrier_interval; }
      SubsecondTime getBarrierInterval() const { return m_barrier_interval; }

      void printState(void);
};

#endif /* __BARRIER_SYNC_SERVER_H__ */
