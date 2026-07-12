#include "address_home_lookup.h"
#include "log.h"
#include "simulator.h"
#include "config.hpp"

// 2026-07-10: static member定義。全AddressHomeLookupインスタンス(コアごと×
// tag directory/dram controller種別ごと)で共有する(header内のコメント参照)。
std::unordered_map<IntPtr, core_id_t> AddressHomeLookup::s_first_touch_node_map;
Lock AddressHomeLookup::s_first_touch_lock;

AddressHomeLookup::AddressHomeLookup(UInt32 ahl_param,
      std::vector<core_id_t>& core_list,
      UInt32 cache_block_size):
   m_ahl_param(ahl_param),
   m_ahl_mask((UInt64(1) << ahl_param) - 1),
   m_core_list(core_list),
   m_cache_block_size(cache_block_size)
{

   // Each Block Address is as follows:
   // /////////////////////////////////////////////////////////// //
   //   block_num               |   block_offset                  //
   // /////////////////////////////////////////////////////////// //

   LOG_ASSERT_ERROR((1 << m_ahl_param) >= (SInt32) m_cache_block_size,
         "2^AHL param(%u) must be >= Cache Block Size(%u)",
         m_ahl_param, m_cache_block_size);
   m_total_modules = core_list.size();

   // 2026-07-10 First-Touch: ノード境界。未設定の他configを壊さないよう、
   // general/cores_per_nodeが無ければ「全コアが1ノード」扱いにフォールバックする
   // (=nodeOfが常に0を返し、実質従来の単一ノード相当の挙動になる)。
   try
   {
      m_cores_per_node = Sim()->getCfg()->getInt("general/cores_per_node");
      if (m_cores_per_node == 0)
         m_cores_per_node = Sim()->getConfig()->getApplicationCores();
   }
   catch (...)
   {
      m_cores_per_node = Sim()->getConfig()->getApplicationCores();
   }
}

AddressHomeLookup::~AddressHomeLookup()
{
   // There is no memory to deallocate, so destructor has no function
}

core_id_t AddressHomeLookup::nodeOf(core_id_t core) const
{
   if (m_cores_per_node == 0)
      return 0;
   return core / (core_id_t) m_cores_per_node;
}

core_id_t AddressHomeLookup::controllerForNode(core_id_t node) const
{
   for (std::vector<core_id_t>::const_iterator it = m_core_list.begin(); it != m_core_list.end(); ++it)
   {
      if (nodeOf(*it) == node)
         return *it;
   }
   // このノードにコントローラが無い場合(例: 該当ノードにアクティブコアが
   // 無いconfig)は先頭にフォールバック。
   return m_core_list[0];
}

core_id_t AddressHomeLookup::getHome(IntPtr address, core_id_t requester) const
{
   if (requester == INVALID_CORE_ID)
   {
      // 従来方式(アドレスハッシュのみ)。requesterを渡さない全ての既存呼び出しの
      // 後方互換フォールバック。
      SInt32 module_num = (address >> m_ahl_param) % m_total_modules;
      LOG_ASSERT_ERROR(0 <= module_num && module_num < (SInt32) m_total_modules, "module_num(%i), total_modules(%u)", module_num, m_total_modules);
      return (m_core_list[module_num]);
   }

   // First-Touch: このアドレスブロックへの初回アクセス時、要求元コアと同じ
   // ノードを記録し、以後は固定する。「最初に触ったノード」はアドレスだけで
   // 決まるグローバルな事実なので、s_first_touch_node_map(static、全インスタンス
   // 共有)で判定する。tag directory用/dram controller用でm_ahl_paramは常に
   // 同じ値(dram_directory_home_lookup_param)で構築されるため、block_keyの
   // 空間は両者で一致している。
   IntPtr block_key = address >> m_ahl_param;

   core_id_t node;
   {
      ScopedLock sl(s_first_touch_lock);

      std::unordered_map<IntPtr, core_id_t>::const_iterator it = s_first_touch_node_map.find(block_key);
      if (it != s_first_touch_node_map.end())
      {
         node = it->second;
      }
      else
      {
         node = nodeOf(requester);
         s_first_touch_node_map[block_key] = node;
      }
   }

   // ノードが決まった後、そのノードを「このインスタンスが持つコアリスト
   // (tag directory用かdram controller用か)」の中でどのコアが代表するかを解決する。
   return controllerForNode(node);
}

IntPtr AddressHomeLookup::getLinearBlock(IntPtr address) const
{
   return (address >> m_ahl_param) / m_total_modules;
}

IntPtr AddressHomeLookup::getLinearAddress(IntPtr address) const
{
   return (getLinearBlock(address) << m_ahl_param) | (address & m_ahl_mask);
}
