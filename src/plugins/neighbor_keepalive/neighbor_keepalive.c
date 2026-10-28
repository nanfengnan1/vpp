/* SPDX-License-Identifier: Apache-2.0
 * Local EVPN MAC/IP liveness. Main-thread ownership; use the existing native
 * ARP/ND validation/learning path, without intercepting or consuming replies.
 */
#include <vlib/vlib.h>
#include <vlibmemory/api.h>
#include <vnet/plugin/plugin.h>
#include <vnet/format_fns.h>
#include <vpp/app/version.h>
#include <vnet/l2/l2_input.h>
#include <vnet/l2/l2_fib.h>
#include <vnet/l2/l2_bd.h>
#include <vnet/ip-neighbor/ip_neighbor.h>
#include <vnet/ip-neighbor/ip4_neighbor.h>
#include <vnet/ip-neighbor/ip6_neighbor.h>
#include <vnet/ip/ip_sas.h>
#include <vnet/fib/fib_sas.h>
#include <vnet/ip/ip_types_api.h>
#include <vnet/ethernet/ethernet_types_api.h>
#include <vppinfra/bihash_24_8.h>
#include <neighbor_keepalive/neighbor_keepalive.api_enum.h>
#include <neighbor_keepalive/neighbor_keepalive.api_types.h>

enum
{
  NK_REACHABLE,
  NK_PROBING,
  NK_FAILED,
  NK_SEND_ERROR
};
#define NK_LIMIT	 4096
#define NK_BUDGET	 512
#define NK_RETRIES	 3
#define NK_RETRY_SECONDS 5

typedef struct
{
  u32 bvi, port;
} nk_scope_t;
typedef struct
{
  ip_address_t ip;
  mac_address_t mac;
  u32 bvi, port, seen;
  u8 state, attempts;
  f64 learned, next, interval, retired;
} nk_target_t;
static struct
{
  u16 msg_id_base;
  u8 enabled;
  u32 cycle;
  nk_scope_t *scopes;
  nk_target_t *targets;
  clib_bihash_24_8_t index;
  u64 probes, confirmed, failed, send_errors, capacity_errors;
} nk;

static clib_bihash_kv_24_8_t
nk_key (const ip_address_t *ip, u32 bvi)
{
  clib_bihash_kv_24_8_t key = { 0 };
  /* IPv4's unused union bytes need not be initialized by all learning paths. */
  if (ip_addr_version (ip) == AF_IP4)
    key.key[0] = ip_addr_v4 (ip).as_u32;
  else
    clib_memcpy (&key.key, &ip_addr_v6 (ip), sizeof (ip6_address_t));
  key.key[2] = ((u64) bvi << 32) | ip_addr_version (ip);
  return key;
}

static int
nk_scope_valid (u32 bvi, u32 port)
{
  vnet_main_t *vnm = vnet_get_main ();
  if (!vnet_sw_interface_is_valid (vnm, bvi) || !vnet_sw_interface_is_valid (vnm, port))
    return 0;
  l2_input_config_t *b = l2input_intf_config (bvi);
  l2_input_config_t *p = l2input_intf_config (port);
  return l2_input_is_bridge (b) && l2_input_is_bvi (b) && l2_input_is_bridge (p) &&
	 !l2_input_is_bvi (p) && b->bd_index == p->bd_index;
}

static int
nk_allowed (u32 bvi, u32 port)
{
  nk_scope_t *s;
  vec_foreach (s, nk.scopes)
    if (s->bvi == bvi && s->port == port)
      return nk_scope_valid (bvi, port);
  return 0;
}

static void
nk_remove (nk_target_t *t)
{
  clib_bihash_kv_24_8_t key = nk_key (&t->ip, t->bvi);
  clib_bihash_add_del_24_8 (&nk.index, &key, 0);
  pool_put (nk.targets, t);
}

/* Only standard native neighbor learning advances learned. Sending a probe,
 * a repeated configuration request, or Linux mirror readback never does. */
static walk_rc_t
nk_learned (index_t ipni, void *ctx)
{
  ip_neighbor_t *n = ip_neighbor_get (ipni);
  if (!(n->ipn_flags & IP_NEIGHBOR_FLAG_DYNAMIC) || (n->ipn_flags & IP_NEIGHBOR_FLAG_STATIC))
    return WALK_CONTINUE;
  u32 bvi = ip_neighbor_get_sw_if_index (n);
  if (!vnet_sw_interface_is_valid (vnet_get_main (), bvi) ||
      !l2_input_is_bvi (l2input_intf_config (bvi)))
    return WALK_CONTINUE;
  clib_bihash_kv_24_8_t key = nk_key (ip_neighbor_get_ip (n), bvi), found;
  nk_target_t *t = NULL;
  if (!clib_bihash_search_24_8 (&nk.index, &key, &found))
    t = pool_elt_at_index (nk.targets, found.value);
  clib_bihash_kv_8_8_t mac_key = { .key = l2fib_make_key ((const u8 *) &n->ipn_mac,
							  l2input_intf_config (bvi)->bd_index) },
		       mac_value;
  l2fib_entry_result_t fdb = { .raw = ~0ULL };
  if (!clib_bihash_search_8_8 (&l2fib_main.mac_table, &mac_key, &mac_value))
    fdb.raw = mac_value.value;
  int local =
    fdb.raw != ~0ULL &&
    !(fdb.fields.flags & (L2FIB_ENTRY_RESULT_FLAG_STATIC | L2FIB_ENTRY_RESULT_FLAG_AGE_NOT |
			  L2FIB_ENTRY_RESULT_FLAG_BVI | L2FIB_ENTRY_RESULT_FLAG_FILTER)) &&
    nk_allowed (bvi, fdb.fields.sw_if_index);
  /* A temporarily aged-out FDB entry must not abandon an in-flight probe.
   * But a move to a remote/static MAC must immediately retire the old owner. */
  if (!local)
    {
      if (t && fdb.raw == ~0ULL && nk_allowed (t->bvi, t->port) &&
	  !mac_address_cmp (&t->mac, &n->ipn_mac))
	t->seen = nk.cycle;
      return WALK_CONTINUE;
    }
  f64 now = vlib_time_now (vlib_get_main ());
  if (!t)
    {
      if (pool_elts (nk.targets) >= NK_LIMIT)
	{
	  nk.capacity_errors++;
	  return WALK_CONTINUE;
	}
      pool_get_zero (nk.targets, t);
      key.value = t - nk.targets;
      clib_bihash_add_del_24_8 (&nk.index, &key, 1);
      t->bvi = bvi;
      t->ip = *ip_neighbor_get_ip (n);
      t->learned = -1;
    }
  t->seen = nk.cycle;
  u32 age = l2input_bd_config (l2input_intf_config (bvi)->bd_index)->mac_age * 60;
  /* Account for the FDB's one-minute timestamp granularity. With the minimum
   * supported age (120s), probe at 30s, leaving room for all three attempts. */
  f64 interval = age ? clib_max (1, (int) (age - 60) / 2) : 150;
  if (t->state == NK_REACHABLE && interval < t->interval)
    t->next = clib_min (t->next, t->learned + interval);
  t->interval = interval;
  if (t->learned != n->ipn_time_last_updated || t->port != fdb.fields.sw_if_index ||
      mac_address_cmp (&t->mac, &n->ipn_mac))
    {
      if (t->state == NK_PROBING || t->state == NK_FAILED)
	nk.confirmed++;
      t->mac = n->ipn_mac;
      t->port = fdb.fields.sw_if_index;
      t->learned = n->ipn_time_last_updated;
      t->state = NK_REACHABLE;
      t->attempts = 0;
      /* Stable small jitter avoids synchronized bursts without delaying expiry. */
      t->next = clib_min (now, t->learned) + t->interval -
		((key.key[0] ^ key.key[1] ^ key.key[2]) % 100) * t->interval / 1000;
    }
  return WALK_CONTINUE;
}

static int
nk_send (nk_target_t *t)
{
  vlib_main_t *vm = vlib_get_main ();
  vnet_main_t *vnm = vnet_get_main ();
  if (!nk_scope_valid (t->bvi, t->port) || !vnet_sw_interface_is_admin_up (vnm, t->port))
    return -1;
  vnet_hw_interface_t *bvi = vnet_get_sup_hw_interface (vnm, t->bvi);
  vnet_hw_interface_t *port = vnet_get_sup_hw_interface (vnm, t->port);
  u32 bi;
  vlib_buffer_t *b;
  vnet_link_t link;
  if (ip_addr_version (&t->ip) == AF_IP4)
    {
      ip4_address_t src;
      if (!(fib_sas4_get (t->bvi, &ip_addr_v4 (&t->ip), &src) ||
	    ip4_sas_by_sw_if_index (t->bvi, &ip_addr_v4 (&t->ip), &src)))
	return -1;
      ethernet_arp_header_t *h =
	vlib_packet_template_get_packet (vm, &ip4_main.ip4_arp_request_packet_template, &bi);
      if (!h)
	return -1;
      mac_address_from_bytes (&h->ip4_over_ethernet[0].mac, bvi->hw_address);
      h->ip4_over_ethernet[0].ip4 = src;
      h->ip4_over_ethernet[1].mac = t->mac;
      h->ip4_over_ethernet[1].ip4 = ip_addr_v4 (&t->ip);
      link = VNET_LINK_ARP;
    }
  else
    {
      ip6_address_t src;
      if (!(fib_sas6_get (t->bvi, &ip_addr_v6 (&t->ip), &src) ||
	    ip6_sas_by_sw_if_index (t->bvi, &ip_addr_v6 (&t->ip), &src)))
	return -1;
      icmp6_neighbor_solicitation_header_t *h =
	vlib_packet_template_get_packet (vm, &ip6_neighbor_packet_template, &bi);
      if (!h)
	return -1;
      h->ip.src_address = src;
      h->ip.dst_address = ip_addr_v6 (&t->ip);
      h->neighbor.target_address = ip_addr_v6 (&t->ip);
      clib_memcpy (h->link_layer_option.ethernet_address, bvi->hw_address, 6);
      h->neighbor.icmp.checksum = 0;
      int bogus;
      h->neighbor.icmp.checksum = ip6_tcp_udp_icmp_compute_checksum (vm, NULL, &h->ip, &bogus);
      if (bogus)
	{
	  vlib_buffer_free (vm, &bi, 1);
	  return -1;
	}
      link = VNET_LINK_IP6;
    }
  b = vlib_get_buffer (vm, bi);
  /* Build the access port rewrite (including subinterface VLAN tags), but use
   * the BVI source MAC. Direct egress cannot flood a probe into VXLAN. */
  u8 *rewrite = ethernet_build_rewrite (vnm, t->port, link, (u8 *) &t->mac);
  if (!rewrite)
    {
      vlib_buffer_free (vm, &bi, 1);
      return -1;
    }
  vlib_buffer_advance (b, -(int) vec_len (rewrite));
  ethernet_header_t *eth = vlib_buffer_get_current (b);
  clib_memcpy (eth, rewrite, vec_len (rewrite));
  clib_memcpy (eth->src_address, bvi->hw_address, 6);
  vec_free (rewrite);
  vnet_buffer (b)->sw_if_index[VLIB_RX] = t->bvi;
  vnet_buffer (b)->sw_if_index[VLIB_TX] = t->port;
  vlib_frame_t *f = vlib_get_frame_to_node (vm, port->output_node_index);
  ((u32 *) vlib_frame_vector_args (f))[0] = bi;
  f->n_vectors = 1;
  vlib_put_frame_to_node (vm, port->output_node_index, f);
  return 0;
}

static uword
nk_process (vlib_main_t *vm, vlib_node_runtime_t *rt, vlib_frame_t *frame)
{
  while (1)
    {
      vlib_process_suspend (vm, 1.0);
      if (!nk.enabled)
	continue;
      ++nk.cycle;
      ip_neighbor_walk (AF_IP4, ~0, nk_learned, NULL);
      ip_neighbor_walk (AF_IP6, ~0, nk_learned, NULL);
      f64 now = vlib_time_now (vm);
      u32 budget = NK_BUDGET;
      nk_target_t *t;
      pool_foreach (t, nk.targets)
	{
	  if (!nk_allowed (t->bvi, t->port) || (t->seen != nk.cycle && t->state != NK_FAILED))
	    {
	      nk_remove (t);
	      continue;
	    }
	  if (t->state == NK_FAILED)
	    {
	      if (now - t->retired > 300)
		nk_remove (t);
	      continue;
	    }
	  if (now < t->next)
	    continue;
	  if (t->attempts == NK_RETRIES)
	    {
	      t->state = NK_FAILED;
	      t->retired = now;
	      nk.failed++;
	      /* ip_neighbor_walk just confirmed the same dynamic binding on
	       * this main thread. Never delete a static/remote neighbor or MAC. */
	      ip_neighbor_del (&t->ip, t->bvi);
	      continue;
	    }
	  if (!budget)
	    continue;
	  --budget;
	  t->next = now + NK_RETRY_SECONDS;
	  if (nk_send (t))
	    {
	      t->state = NK_SEND_ERROR;
	      nk.send_errors++;
	      continue;
	    }
	  t->state = NK_PROBING;
	  t->attempts++;
	  nk.probes++;
	}
    }
  return 0;
}
VLIB_REGISTER_NODE (neighbor_keepalive_process_node) = {
  .function = nk_process,
  .type = VLIB_NODE_TYPE_PROCESS,
  .name = "neighbor-keepalive-process",
};

#define REPLY_MSG_ID_BASE nk.msg_id_base
#include <vlibapi/api_helper_macros.h>

static void
vl_api_neighbor_keepalive_set_t_handler (vl_api_neighbor_keepalive_set_t *mp)
{
  vl_api_neighbor_keepalive_set_reply_t *rmp;
  int rv = 0;
  nk_scope_t *s;
  if (mp->enabled)
    vec_foreach (s, nk.scopes)
      if (nk_scope_valid (s->bvi, s->port) &&
	  l2input_bd_config (l2input_intf_config (s->bvi)->bd_index)->mac_age == 1)
	rv = VNET_API_ERROR_INVALID_VALUE;
  if (!rv)
    {
      nk.enabled = mp->enabled;
      if (!nk.enabled)
	{
	  nk_target_t *t;
	  pool_foreach (t, nk.targets)
	    nk_remove (t);
	}
    }
  REPLY_MACRO (VL_API_NEIGHBOR_KEEPALIVE_SET_REPLY);
}
static void
vl_api_neighbor_keepalive_status_t_handler (vl_api_neighbor_keepalive_status_t *mp)
{
  vl_api_neighbor_keepalive_status_reply_t *rmp;
  int rv = 0;
  REPLY_MACRO2 (VL_API_NEIGHBOR_KEEPALIVE_STATUS_REPLY, ({
		  rmp->enabled = nk.enabled;
		  rmp->targets = htonl (pool_elts (nk.targets));
		  rmp->probes = clib_host_to_net_u64 (nk.probes);
		  rmp->confirmed = clib_host_to_net_u64 (nk.confirmed);
		  rmp->failed = clib_host_to_net_u64 (nk.failed);
		  rmp->send_errors = clib_host_to_net_u64 (nk.send_errors);
		  rmp->capacity_errors = clib_host_to_net_u64 (nk.capacity_errors);
		}));
}
static void
vl_api_neighbor_keepalive_scope_add_del_t_handler (vl_api_neighbor_keepalive_scope_add_del_t *mp)
{
  vl_api_neighbor_keepalive_scope_add_del_reply_t *rmp;
  u32 bvi = ntohl (mp->bvi), port = ntohl (mp->port);
  int rv = 0, found = 0;
  if (mp->is_add &&
      (!nk_scope_valid (bvi, port) ||
       (nk.enabled && l2input_bd_config (l2input_intf_config (bvi)->bd_index)->mac_age == 1)))
    rv = VNET_API_ERROR_INVALID_VALUE;
  if (!rv)
    {
      for (u32 i = 0; i < vec_len (nk.scopes); i++)
	if (nk.scopes[i].bvi == bvi && nk.scopes[i].port == port)
	  {
	    found = 1;
	    if (!mp->is_add)
	      vec_del1 (nk.scopes, i);
	    break;
	  }
      if (mp->is_add && !found)
	{
	  nk_scope_t s = { bvi, port };
	  vec_add1 (nk.scopes, s);
	}
    }
  REPLY_MACRO (VL_API_NEIGHBOR_KEEPALIVE_SCOPE_ADD_DEL_REPLY);
}
static void
vl_api_neighbor_keepalive_scope_dump_t_handler (vl_api_neighbor_keepalive_scope_dump_t *mp)
{
  vl_api_registration_t *reg = vl_api_client_index_to_registration (mp->client_index);
  if (!reg)
    return;
  nk_scope_t *s;
  vec_foreach (s, nk.scopes)
    {
      vl_api_neighbor_keepalive_scope_details_t *rmp = vl_msg_api_alloc_zero (sizeof (*rmp));
      rmp->_vl_msg_id = htons (VL_API_NEIGHBOR_KEEPALIVE_SCOPE_DETAILS + nk.msg_id_base);
      rmp->context = mp->context;
      rmp->bvi = htonl (s->bvi);
      rmp->port = htonl (s->port);
      vl_api_send_msg (reg, (u8 *) rmp);
    }
}
static void
vl_api_neighbor_keepalive_dump_t_handler (vl_api_neighbor_keepalive_dump_t *mp)
{
  vl_api_registration_t *reg = vl_api_client_index_to_registration (mp->client_index);
  if (!reg)
    return;
  nk_target_t *t;
  f64 now = vlib_time_now (vlib_get_main ());
  pool_foreach (t, nk.targets)
    {
      vl_api_neighbor_keepalive_details_t *rmp = vl_msg_api_alloc_zero (sizeof (*rmp));
      rmp->_vl_msg_id = htons (VL_API_NEIGHBOR_KEEPALIVE_DETAILS + nk.msg_id_base);
      rmp->context = mp->context;
      rmp->bvi = htonl (t->bvi);
      rmp->port = htonl (t->port);
      ip_address_encode2 (&t->ip, &rmp->address);
      mac_address_encode (&t->mac, rmp->mac);
      rmp->state = t->state;
      rmp->attempts = t->attempts;
      rmp->age = htonl (clib_max (0, now - t->learned));
      rmp->next_probe = htonl (clib_max (0, t->next - now));
      rmp->interval = htonl (t->interval);
      vl_api_send_msg (reg, (u8 *) rmp);
    }
}
#include <neighbor_keepalive/neighbor_keepalive.api.c>
static clib_error_t *
nk_show (vlib_main_t *vm, unformat_input_t *input, vlib_cli_command_t *cmd)
{
  static const char *states[] = { "reachable", "probing", "failed", "send-error" };
  vlib_cli_output (vm,
		   "%s: targets %u probes %llu confirmed %llu failed %llu "
		   "send-errors %llu capacity-errors %llu",
		   nk.enabled ? "enabled" : "disabled", pool_elts (nk.targets), nk.probes,
		   nk.confirmed, nk.failed, nk.send_errors, nk.capacity_errors);
  nk_target_t *t;
  pool_foreach (t, nk.targets)
    vlib_cli_output (vm, "bvi %u port %u %U %U %s attempts %u age %.1f", t->bvi, t->port,
		     format_ip_address, &t->ip, format_mac_address_t, &t->mac, states[t->state],
		     t->attempts, vlib_time_now (vm) - t->learned);
  return NULL;
}
VLIB_CLI_COMMAND (neighbor_keepalive_show_command, static) = {
  .path = "show neighbor-keepalive",
  .short_help = "show neighbor-keepalive",
  .function = nk_show,
};

static clib_error_t *
nk_init (vlib_main_t *vm)
{
  nk.msg_id_base = setup_message_id_table ();
  clib_bihash_init_24_8 (&nk.index, "neighbor-keepalive", 1024, 0);
  return NULL;
}
VLIB_INIT_FUNCTION (nk_init);
VLIB_PLUGIN_REGISTER () = {
  .version = VPP_BUILD_VER,
  .description = "Local dynamic ARP/ND neighbor keepalive",
};
