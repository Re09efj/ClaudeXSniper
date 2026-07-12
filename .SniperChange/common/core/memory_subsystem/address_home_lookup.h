#ifndef __ADDRESS_HOME_LOOKUP_H__
#define __ADDRESS_HOME_LOOKUP_H__

#include <vector>
#include <unordered_map>

#include "fixed_types.h"
#include "lock.h"

/*
 * TODO abstract MMU stuff to a configure file to allow
 * user to specify number of memory controllers, and
 * the address space that each is in charge of.  Default behavior:
 * AHL assumes that every core has a memory controller,
 * and that each shares an equal number of address spaces.
 *
 * Each core has an AHL, but they must keep their data consistent
 * regarding boundaries!
 *
 * Maybe allow the ability to have public and private memory space?
 *
 * 2026-07-10 First-Touch追加 (ClaudeXSniper):
 * requester(要求元コア)を渡した場合、そのアドレスブロックへの最初のアクセス時に
 * 要求元コアと同じノードのコントローラをホームとして記録し、以後はその記録に
 * 従う(first-touch NUMAポリシー相当)。requesterを渡さない/INVALID_CORE_IDの
 * 場合は従来通りアドレスハッシュのみで決める(後方互換のフォールバック)。
 * ノード境界はgeneral/cores_per_node(config)から読む。
 *
 * 重要: AddressHomeLookupはコアごと(MemoryManagerBase単位)に別々のインスタンスが
 * 生成される(tag directory用・dram controller用それぞれ、実質コア数×2個)。
 * 「あるアドレスをどのノードが最初に触ったか」はアドレスだけで決まるべき
 * グローバルな事実なので、first-touchの記録(s_first_touch_node_map)は
 * インスタンスごとではなく static(プロセス全体で1つ共有)にしている。
 * インスタンスごとに違うのは「そのノードを具体的にどのコアが代表するか」
 * (m_core_list、tag directoryとdram controllerで違いうる)だけで、これは
 * controllerForNode()でインスタンスごとに解決する。
 */

class AddressHomeLookup
{
   public:
      AddressHomeLookup(UInt32 ahl_param,
            std::vector<core_id_t>& core_list,
            UInt32 cache_block_size);
      ~AddressHomeLookup();
      // Return home node for a given address. requesterを渡すとfirst-touch方式になる。
      core_id_t getHome(IntPtr address, core_id_t requester = INVALID_CORE_ID) const;
      // Within home node, return unique, incrementing block number
      IntPtr getLinearBlock(IntPtr address) const;
      // Within home node, return unique, incrementing address to be used in cache set selection
      IntPtr getLinearAddress(IntPtr address) const;

   private:
      core_id_t nodeOf(core_id_t core) const;
      core_id_t controllerForNode(core_id_t node) const;

      UInt32 m_ahl_param;
      UInt64 m_ahl_mask;
      std::vector<core_id_t> m_core_list;
      UInt32 m_total_modules;
      UInt32 m_cache_block_size;

      UInt32 m_cores_per_node;

      // static: 全AddressHomeLookupインスタンス(コアごと×tag dir/dram cntlr種別ごと)
      // で共有する。「アドレスブロック→最初に触ったノード」はグローバルな事実であり、
      // インスタンスごとに独立させると同じアドレスに対してインスタンスごとに違う
      // ノードを「最初」と判定してしまい、コヒーレンシ上も統計上も矛盾が生じる。
      static std::unordered_map<IntPtr, core_id_t> s_first_touch_node_map;
      static Lock s_first_touch_lock;
};

#endif /* __ADDRESS_HOME_LOOKUP_H__ */
