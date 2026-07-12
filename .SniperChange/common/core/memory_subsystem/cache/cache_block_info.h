#ifndef __CACHE_BLOCK_INFO_H__
#define __CACHE_BLOCK_INFO_H__

#include "fixed_types.h"
#include "cache_state.h"
#include "cache_base.h"

#include <atomic>

class CacheBlockInfo
{
   public:
      enum option_t
      {
         PREFETCH,
         WARMUP,
         NUM_OPTIONS
      };

      static const UInt8 BitsUsedOffset = 3;  // Track usage on 1<<BitsUsedOffset granularity (per 64-bit / 8-byte)
      typedef UInt8 BitsUsedType;      // Enough to store one bit per 1<<BitsUsedOffset byte element per cache line (8 8-byte elements for 64-byte cache lines)

   // This can be extended later to include other information
   // for different cache coherence protocols
   private:
      // 2026-07-12: m_tag/m_cstate are read by a core's own processMemOpFromCore()
      // (protected by that core's own per-address SetLock via acquireLock/
      // acquireStackLock) but are *written* cross-thread by CacheCntlr::
      // updateCacheBlock()/invalidateCacheBlock() when a *different* core's
      // cache level (e.g. a shared LLC) invalidates a sibling core's private
      // L1 block directly via a plain C++ call on that sibling's CacheCntlr
      // object (see cache_cntlr.cc processShmemReqFromPrevCache's write-hit
      // invalidate loop and updateCacheBlock's own recursive prev_cache_cntlrs
      // loop). That cross-controller call does not, and structurally cannot
      // safely, acquire the target's own SetLock (doing so would require
      // acquiring locks in an order that is not always consistent across the
      // cache hierarchy and can deadlock, e.g. core A holding its own L1 lock
      // + the shared LLC lock while trying to acquire core B's L1 lock, with
      // core B simultaneously holding its L1 lock while blocked waiting on
      // the LLC lock held by A).
      //
      // ThreadSanitizer confirmed this as a genuine data race (two distinct
      // mutexes protecting the two sides) during the LU/2TH/W silent-hang
      // investigation (see Documents/SniperBugFix.md). LU's NPB SSOR pipeline
      // busy-polls a shared flag with no lock of its own (relying on cache
      // coherence + memory ordering alone, as real hardware would), so a
      // spinning core that never observes another core's invalidate/update
      // here can spin forever.
      //
      // Fix: make these two fields atomic (seq_cst, see below) instead of
      // adding new locking. This requires no change to *when*
      // updates happen (the existing, if imperfect, locking discipline is
      // untouched) -- it only guarantees that whichever value is currently
      // set is properly published/observed across threads, which is exactly
      // what a spin-polling reader needs and cannot deadlock since atomics
      // never block. All copying already goes through the explicit clone()/
      // invalidate() methods (not implicit copy-construction or memcpy of
      // the whole object -- CacheSet stores CacheBlockInfo* pointers,
      // individually heap-allocated via create()), so adding non-copyable
      // std::atomic members here is safe.
      std::atomic<IntPtr> m_tag;
      std::atomic<CacheState::cstate_t> m_cstate;
      UInt64 m_owner;
      BitsUsedType m_used;
      UInt8 m_options;  // large enough to hold a bitfield for all available option_t's

      static const char* option_names[];

   public:
      CacheBlockInfo(IntPtr tag = ~0,
            CacheState::cstate_t cstate = CacheState::INVALID,
            UInt64 options = 0);
      virtual ~CacheBlockInfo();

      static CacheBlockInfo* create(CacheBase::cache_t cache_type);

      virtual void invalidate(void);
      virtual void clone(CacheBlockInfo* cache_block_info);

      // 2026-07-12: memory_order_acquire/release (matched per-variable) is NOT
      // enough here: invalidate() writes m_tag then m_cstate (two *separate*
      // atomics), and readers (CacheSet::find() reading getTag(), followed by
      // operationPermissibleinCache() reading getCState() on the same
      // object) read them in the same relative order. Per-variable
      // release/acquire only guarantees a happens-before edge for that one
      // variable; it does NOT guarantee that a reader who observes the new
      // m_tag will also observe the new m_cstate (classic multi-variable
      // ordering pitfall, IRIW-style). Using the default seq_cst ordering
      // (a single total order across all atomic ops) closes that gap. The
      // extra fence cost is negligible relative to the rest of the
      // simulator's per-instruction work.
      bool isValid() const { return (m_tag.load() != ((IntPtr) ~0)); }

      IntPtr getTag() const { return m_tag.load(); }
      CacheState::cstate_t getCState() const { return m_cstate.load(); }

      void setTag(IntPtr tag) { m_tag.store(tag); }
      void setCState(CacheState::cstate_t cstate) { m_cstate.store(cstate); }

      UInt64 getOwner() const { return m_owner; }
      void setOwner(UInt64 owner) { m_owner = owner; }

      bool hasOption(option_t option) { return m_options & (1 << option); }
      void setOption(option_t option) { m_options |= (1 << option); }
      void clearOption(option_t option) { m_options &= ~(UInt64(1) << option); }

      BitsUsedType getUsage() const { return m_used; };
      bool updateUsage(UInt32 offset, UInt32 size);
      bool updateUsage(BitsUsedType used);

      static const char* getOptionName(option_t option);
};

class CacheCntlr
{
   public:
      virtual bool isInLowerLevelCache(CacheBlockInfo *block_info) { return false; }
      virtual void incrementQBSLookupCost() {}
};

#endif /* __CACHE_BLOCK_INFO_H__ */
