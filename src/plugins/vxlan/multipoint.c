/* SPDX-License-Identifier: Apache-2.0
 * Native source-only Ethernet VXLAN. IPv4/IPv6 underlay and payload; software checksum/GSO
 * fallback.
 */
#include <vxlan/vxlan.h>
#include <vnet/interface_output.h>
#include <vnet/gso/gso.h>

static uword
mac_key (const u8 *mac)
{
  u64 key = 0;
  clib_memcpy_fast (&key, mac, 6);
  return key;
}

int
vxlan_mp_remote_update (u32 sw_if_index, const u8 *mac, ip46_address_t remote, u8 bum, u8 is_add)
{
  u32 ti = vnet_vxlan_get_tunnel_index (sw_if_index);
  if (ti == ~0)
    return VNET_API_ERROR_INVALID_SW_IF_INDEX;
  vxlan_tunnel_t *t = pool_elt_at_index (vxlan_main.tunnels, ti);
  uword key = mac_key (mac);
  if (!t->multipoint || ip46_address_is_zero (&remote) || ip46_address_is_multicast (&remote) ||
      ip46_address_is_ip4 (&remote) != ip46_address_is_ip4 (&t->src) ||
      (ip46_address_is_ip4 (&remote) && remote.ip4.as_u32 == ~0) ||
      !ip46_address_cmp (&remote, &t->src) || (bum ? key != 0 : (key == 0 || (mac[0] & 1))))
    return VNET_API_ERROR_INVALID_VALUE;

  vlib_main_t *vm = vlib_get_main ();
  vlib_worker_thread_barrier_sync (vm);
  if (bum)
    {
      u32 i;
      for (i = 0; i < vec_len (t->flood_remotes); i++)
	if (!ip46_address_cmp (&t->flood_remotes[i], &remote))
	  break;
      if (is_add && i == vec_len (t->flood_remotes))
	vec_add1 (t->flood_remotes, remote);
      else if (!is_add && i < vec_len (t->flood_remotes))
	vec_del1 (t->flood_remotes, i);
    }
  else if (is_add)
    {
      uword *value = hash_get (t->remote_by_mac, key);
      ip46_address_t *slot;
      if (value)
	slot = pool_elt_at_index (t->remote_ips, value[0]);
      else
	{
	  pool_get (t->remote_ips, slot);
	  hash_set (t->remote_by_mac, key, slot - t->remote_ips);
	}
      *slot = remote;
    }
  else
    {
      uword *value = hash_get (t->remote_by_mac, key);
      /* A delayed withdrawal must not delete a newer MAC move. */
      if (value && !ip46_address_cmp (&t->remote_ips[value[0]], &remote))
	{
	  pool_put_index (t->remote_ips, value[0]);
	  hash_unset (t->remote_by_mac, key);
	}
    }
  vlib_worker_thread_barrier_release (vm);
  return 0;
}

enum
{
  MP_NEXT_DROP,
  MP_NEXT_LOOKUP4,
  MP_NEXT_LOOKUP6,
  MP_N_NEXT
};
enum
{
  MP_NO_REMOTE,
  MP_NO_BUFFER,
  MP_INVALID_PACKET,
  MP_SEGMENTED,
  MP_N_ERRORS
};
static char *mp_errors[] = { "no remote VTEP", "replication buffer allocation failed",
			     "invalid or oversized inner frame", "TCP GSO packets segmented" };

typedef struct
{
  u32 sw_if_index, vni;
  ip46_address_t remote;
} mp_trace_t;

static u8 *
format_mp_trace (u8 *s, va_list *args)
{
  va_arg (*args, vlib_main_t *);
  va_arg (*args, vlib_node_t *);
  mp_trace_t *tr = va_arg (*args, mp_trace_t *);
  return format (s, "VXLAN multipoint sw-if %u vni %u remote %U", tr->sw_if_index, tr->vni,
		 format_ip46_address, &tr->remote, IP46_TYPE_ANY);
}

/* The generic IPv6 checksum helper only handles a subset of extension
 * headers. Use the parsed transport offset for inner TCP/UDP offloads. */
static void
mp_inner_checksums (vlib_main_t *vm, vlib_buffer_t *b)
{
  if ((b->flags & (VNET_BUFFER_F_IS_IP6 | VNET_BUFFER_F_OFFLOAD)) ==
      (VNET_BUFFER_F_IS_IP6 | VNET_BUFFER_F_OFFLOAD))
    {
      ip6_header_t *ip = (void *) (b->data + vnet_buffer (b)->l3_hdr_offset);
      u32 hlen = vnet_buffer (b)->l4_hdr_offset - vnet_buffer (b)->l3_hdr_offset;
      u32 len = clib_net_to_host_u16 (ip->payload_length) + sizeof (*ip) - hlen;
      u8 *l4 = b->data + vnet_buffer (b)->l4_hdr_offset;
      u8 tcp = !!(vnet_buffer (b)->oflags & VNET_BUFFER_OFFLOAD_F_TCP_CKSUM);
      u8 udp = !!(vnet_buffer (b)->oflags & VNET_BUFFER_OFFLOAD_F_UDP_CKSUM);
      if (tcp || udp)
	{
	  u16 *field = tcp ? &((tcp_header_t *) l4)->checksum : &((udp_header_t *) l4)->checksum;
	  *field = 0;
	  ip_csum_t sum = clib_host_to_net_u16 (len) +
			  clib_host_to_net_u16 (tcp ? IP_PROTOCOL_TCP : IP_PROTOCOL_UDP);
	  for (u32 i = 0; i < ARRAY_LEN (ip->src_address.as_uword); i++)
	    {
	      sum =
		ip_csum_with_carry (sum, clib_mem_unaligned (&ip->src_address.as_uword[i], uword));
	      sum =
		ip_csum_with_carry (sum, clib_mem_unaligned (&ip->dst_address.as_uword[i], uword));
	    }
	  *field = ip_calculate_l4_checksum (vm, b, sum, len, (u8 *) ip, hlen, 0);
	  if (udp && !*field)
	    *field = 0xffff;
	}
      vnet_buffer_offload_flags_clear (b, VNET_BUFFER_OFFLOAD_F_IP_CKSUM |
					    VNET_BUFFER_OFFLOAD_F_TCP_CKSUM |
					    VNET_BUFFER_OFFLOAD_F_UDP_CKSUM);
    }
  else
    vnet_calc_checksums_inline (vm, b, b->flags & VNET_BUFFER_F_IS_IP4, 0);
}

static void
mp_send (vlib_main_t *vm, vlib_node_runtime_t *node, vlib_buffer_t *b, vxlan_tunnel_t *t,
	 ip46_address_t remote)
{
  u32 hash = vnet_l2_compute_flow_hash (b);
  /* Finish checksums in software. Do not pass partially checked inner
   * headers or unsupported tunnel TSO flags to an arbitrary underlay NIC. */
  mp_inner_checksums (vm, b);
  u8 ip4 = ip46_address_is_ip4 (&t->src);
  u16 hlen = ip4 ? sizeof (ip4_vxlan_header_t) : sizeof (ip6_vxlan_header_t);
  vlib_buffer_advance (b, -(int) hlen);
  void *header = vlib_buffer_get_current (b);
  clib_memset (header, 0, hlen);
  u32 len = vlib_buffer_length_in_chain (vm, b);
  udp_header_t *udp;
  vxlan_header_t *vxlan;
  if (ip4)
    {
      ip4_vxlan_header_t *h = header;
      h->ip4.ip_version_and_header_length = 0x45;
      h->ip4.ttl = 64;
      h->ip4.protocol = IP_PROTOCOL_UDP;
      h->ip4.src_address = t->src.ip4;
      h->ip4.dst_address = remote.ip4;
      h->ip4.length = clib_host_to_net_u16 (len);
      h->ip4.checksum = ip4_header_checksum (&h->ip4);
      udp = &h->udp;
      vxlan = &h->vxlan;
    }
  else
    {
      ip6_vxlan_header_t *h = header;
      h->ip6.ip_version_traffic_class_and_flow_label = clib_host_to_net_u32 (6 << 28);
      h->ip6.hop_limit = 64;
      h->ip6.protocol = IP_PROTOCOL_UDP;
      h->ip6.src_address = t->src.ip6;
      h->ip6.dst_address = remote.ip6;
      h->ip6.payload_length = clib_host_to_net_u16 (len - sizeof (ip6_header_t));
      udp = &h->udp;
      vxlan = &h->vxlan;
    }
  udp->src_port = clib_host_to_net_u16 (49152 | (hash & 0x3fff));
  udp->dst_port = clib_host_to_net_u16 (t->dst_port);
  udp->length = clib_host_to_net_u16 (len - (ip4 ? sizeof (ip4_header_t) : sizeof (ip6_header_t)));
  vxlan->flags = VXLAN_FLAGS_I;
  vxlan->vni_reserved = clib_host_to_net_u32 (t->vni << 8);
  if (!ip4)
    {
      int bogus = 0;
      udp->checksum = ip6_tcp_udp_icmp_compute_checksum (vm, b, header, &bogus);
      ASSERT (!bogus);
      if (!udp->checksum)
	udp->checksum = 0xffff;
    }
  vnet_buffer (b)->sw_if_index[VLIB_TX] = t->encap_fib_index;
  vnet_buffer (b)->ip.flow_hash = hash;
  vnet_buffer (b)->l3_hdr_offset = b->current_data;
  vnet_buffer (b)->l4_hdr_offset =
    b->current_data + (ip4 ? sizeof (ip4_header_t) : sizeof (ip6_header_t));
  b->flags &= ~(VNET_BUFFER_F_IS_IP4 | VNET_BUFFER_F_IS_IP6 | VNET_BUFFER_F_L2_HDR_OFFSET_VALID |
		VNET_BUFFER_F_L4_CHECKSUM_COMPUTED | VNET_BUFFER_F_L4_CHECKSUM_CORRECT);
  b->flags |= (ip4 ? VNET_BUFFER_F_IS_IP4 : VNET_BUFFER_F_IS_IP6) |
	      VNET_BUFFER_F_L3_HDR_OFFSET_VALID | VNET_BUFFER_F_L4_HDR_OFFSET_VALID;
  vlib_increment_combined_counter (
    &vnet_get_main ()->interface_main.combined_sw_if_counters[VNET_INTERFACE_COUNTER_TX],
    vlib_get_thread_index (), t->sw_if_index, 1, len);
  if (b->flags & VLIB_BUFFER_IS_TRACED)
    {
      mp_trace_t *tr = vlib_add_trace (vm, node, b, sizeof (*tr));
      *tr = (mp_trace_t){ t->sw_if_index, t->vni, remote };
    }
  u32 bi = vlib_get_buffer_index (vm, b);
  /* Native underlay FIB lookup tracks route changes without remote tunnel
   * objects or per-remote interfaces. The tenant FIB is never used here. */
  vlib_buffer_enqueue_to_single_next (vm, node, &bi, ip4 ? MP_NEXT_LOOKUP4 : MP_NEXT_LOOKUP6, 1);
}

/* Parse actual inner headers, not flags left from an outer tunnel.
 * Bounds checks precede every dereference. Non-IP Ethernet frames need no
 * checksum work. Fragmented packets cannot be TCP GSO templates. */
static int
mp_inner_headers (vlib_main_t *vm, vlib_buffer_t *b, u8 *protocol)
{
  u8 *data = vlib_buffer_get_current (b);
  u32 n = b->current_length, off = sizeof (ethernet_header_t);
  if (n < off)
    return 0;
  u16 type = clib_net_to_host_u16 (((ethernet_header_t *) data)->type);
  for (int i = 0; type == 0x8100 || type == 0x88a8; i++)
    {
      if (i == 4 || n < off + 4)
	return 0;
      type = clib_net_to_host_u16 (((ethernet_vlan_header_t *) (data + off))->type);
      off += 4;
    }
  b->flags &= ~(VNET_BUFFER_F_IS_IP4 | VNET_BUFFER_F_IS_IP6);
  vnet_buffer (b)->l2_hdr_offset = b->current_data;
  vnet_buffer (b)->l3_hdr_offset = b->current_data + off;
  if (type == ETHERNET_TYPE_IP4)
    {
      if (n < off + sizeof (ip4_header_t))
	return 0;
      ip4_header_t *ip = (void *) (data + off);
      u32 ihl = ip4_header_bytes (ip);
      if ((ip->ip_version_and_header_length >> 4) != 4 || ihl < 20 || n < off + ihl)
	return 0;
      if ((b->flags & VNET_BUFFER_F_GSO) && ip4_is_fragment (ip))
	return 0;
      *protocol = ip->protocol;
      off += ihl;
      b->flags |= VNET_BUFFER_F_IS_IP4;
    }
  else if (type == ETHERNET_TYPE_IP6)
    {
      if (n < off + sizeof (ip6_header_t))
	return 0;
      if ((data[off] >> 4) != 6)
	return 0;
      *protocol = ((ip6_header_t *) (data + off))->protocol;
      off += sizeof (ip6_header_t);
      for (int i = 0; *protocol == 0 || *protocol == 43 || *protocol == 60 || *protocol == 44 ||
		      *protocol == 51;
	   i++)
	{
	  if (i == 8 || n < off + 8)
	    return 0;
	  u32 len = *protocol == 44 ?
		      8 :
		      (*protocol == 51 ? (data[off + 1] + 2) * 4 : (data[off + 1] + 1) * 8);
	  if (n < off + len || ((b->flags & VNET_BUFFER_F_GSO) && *protocol == 44))
	    return 0;
	  *protocol = data[off];
	  off += len;
	}
      b->flags |= VNET_BUFFER_F_IS_IP6;
    }
  else
    return !(b->flags & (VNET_BUFFER_F_GSO | VNET_BUFFER_F_OFFLOAD));
  vnet_buffer (b)->l4_hdr_offset = b->current_data + off;
  b->flags |= VNET_BUFFER_F_L2_HDR_OFFSET_VALID | VNET_BUFFER_F_L3_HDR_OFFSET_VALID |
	      VNET_BUFFER_F_L4_HDR_OFFSET_VALID;
  if (b->flags & VNET_BUFFER_F_GSO)
    {
      if (*protocol != IP_PROTOCOL_TCP || n < off + sizeof (tcp_header_t))
	return 0;
      u32 th = tcp_header_bytes ((tcp_header_t *) (data + off));
      if (th < 20 || n < off + th || !vnet_buffer2 (b)->gso_size)
	return 0;
      vnet_buffer2 (b)->gso_l4_hdr_sz = th;
    }
  else if (b->flags & VNET_BUFFER_F_OFFLOAD)
    {
      u8 tcp = !!(vnet_buffer (b)->oflags & VNET_BUFFER_OFFLOAD_F_TCP_CKSUM);
      u8 udp = !!(vnet_buffer (b)->oflags & VNET_BUFFER_OFFLOAD_F_UDP_CKSUM);
      u32 size = tcp ? sizeof (tcp_header_t) : (udp ? sizeof (udp_header_t) : 0);
      if ((tcp && (*protocol != IP_PROTOCOL_TCP || udp)) || (udp && *protocol != IP_PROTOCOL_UDP) ||
	  n < off + size)
	return 0;
      if (b->flags & VNET_BUFFER_F_IS_IP6)
	{
	  ip6_header_t *ip = (void *) (b->data + vnet_buffer (b)->l3_hdr_offset);
	  u32 end = vnet_buffer (b)->l3_hdr_offset - b->current_data + sizeof (*ip) +
		    clib_net_to_host_u16 (ip->payload_length);
	  if (end < off + size || end > vlib_buffer_length_in_chain (vm, b))
	    return 0;
	}
    }
  return 1;
}

static void
mp_forward_one (vlib_main_t *vm, vlib_node_runtime_t *node, u32 bi, vxlan_tunnel_t *t)
{
  vlib_buffer_t *b = vlib_get_buffer (vm, bi);
  u32 error = MP_NO_REMOTE;
  u16 hlen =
    ip46_address_is_ip4 (&t->src) ? sizeof (ip4_vxlan_header_t) : sizeof (ip6_vxlan_header_t);
  if (vlib_buffer_length_in_chain (vm, b) > 65535 - hlen ||
      b->current_data < (int) hlen - VLIB_BUFFER_PRE_DATA_SIZE)
    {
      error = MP_INVALID_PACKET;
      goto drop;
    }
  ethernet_header_t *eth = vlib_buffer_get_current (b);
  uword *remote = hash_get (t->remote_by_mac, mac_key (eth->dst_address));
  if (remote && !(eth->dst_address[0] & 1))
    {
      mp_send (vm, node, b, t, t->remote_ips[remote[0]]);
      return;
    }
  u32 count = vec_len (t->flood_remotes);
  if (!count)
    goto drop;
  for (u32 j = 1; j < count; j++)
    {
      vlib_buffer_t *copy = vlib_buffer_copy (vm, b);
      if (copy)
	mp_send (vm, node, copy, t, t->flood_remotes[j]);
      else
	vlib_node_increment_counter (vm, node->node_index, MP_NO_BUFFER, 1);
    }
  mp_send (vm, node, b, t, t->flood_remotes[0]);
  return;
drop:
  b->error = node->errors[error];
  vlib_buffer_enqueue_to_single_next (vm, node, &bi, MP_NEXT_DROP, 1);
}

VLIB_NODE_FN (vxlan4_mp_encap_node)
(vlib_main_t *vm, vlib_node_runtime_t *node, vlib_frame_t *frame)
{
  u32 *from = vlib_frame_vector_args (frame);
  vnet_interface_per_thread_data_t *ptd =
    vec_elt_at_index (vnet_get_main ()->interface_main.per_thread_data, vlib_get_thread_index ());
  for (u32 i = 0; i < frame->n_vectors; i++)
    {
      u32 bi = from[i], error = MP_INVALID_PACKET;
      vlib_buffer_t *b = vlib_get_buffer (vm, bi);
      u32 ti = vnet_vxlan_get_tunnel_index (vnet_buffer (b)->sw_if_index[VLIB_TX]);
      u8 proto = 0;
      if (ti == ~0 || !mp_inner_headers (vm, b, &proto))
	goto drop;
      vxlan_tunnel_t *t = pool_elt_at_index (vxlan_main.tunnels, ti);
      if (b->flags & VNET_BUFFER_F_GSO)
	{
	  u32 hdr =
	    vnet_buffer (b)->l4_hdr_offset - b->current_data + vnet_buffer2 (b)->gso_l4_hdr_sz;
	  if (hdr >= vlib_buffer_get_default_data_size (vm) ||
	      vlib_buffer_length_in_chain (vm, b) <= hdr)
	    goto drop;
	  vnet_buffer_offload_flags_set (
	    b, VNET_BUFFER_OFFLOAD_F_TCP_CKSUM |
		 ((b->flags & VNET_BUFFER_F_IS_IP4) ? VNET_BUFFER_OFFLOAD_F_IP_CKSUM : 0));
	  vec_reset_length (ptd->split_buffers);
	  if (!gso_segment_buffer_inline (vm, ptd, b, 1))
	    {
	      error = MP_NO_BUFFER;
	      goto drop;
	    }
	  for (u32 j = 0; j < vec_len (ptd->split_buffers); j++)
	    {
	      vlib_buffer_t *seg = vlib_get_buffer (vm, ptd->split_buffers[j]);
	      if ((seg->flags & VNET_BUFFER_F_IS_IP6) &&
		  vnet_buffer (seg)->l4_hdr_offset - vnet_buffer (seg)->l3_hdr_offset >
		    sizeof (ip6_header_t))
		{
		  /* The shared GSO helper assumes a base IPv6 header. Repair
		   * length/checksum for the preserved extension header chain. */
		  ip6_header_t *ip = (void *) (seg->data + vnet_buffer (seg)->l3_hdr_offset);
		  ip->payload_length = clib_host_to_net_u16 (
		    seg->current_length - (vnet_buffer (seg)->l3_hdr_offset - seg->current_data) -
		    sizeof (*ip));
		  vnet_buffer_offload_flags_set (seg, VNET_BUFFER_OFFLOAD_F_TCP_CKSUM);
		}
	      mp_forward_one (vm, node, ptd->split_buffers[j], t);
	    }
	  vec_reset_length (ptd->split_buffers);
	  vlib_buffer_free_one (vm, bi);
	  vlib_node_increment_counter (vm, node->node_index, MP_SEGMENTED, 1);
	}
      else
	mp_forward_one (vm, node, bi, t);
      continue;
    drop:
      b->error = node->errors[error];
      vlib_buffer_enqueue_to_single_next (vm, node, &bi, MP_NEXT_DROP, 1);
    }
  return frame->n_vectors;
}

VLIB_REGISTER_NODE (vxlan4_mp_encap_node) = {
  .name = "vxlan-mp-encap",
  .vector_size = sizeof (u32),
  .type = VLIB_NODE_TYPE_INTERNAL,
  .format_trace = format_mp_trace,
  .n_errors = MP_N_ERRORS,
  .error_strings = mp_errors,
  .n_next_nodes = MP_N_NEXT,
  .next_nodes = { [MP_NEXT_DROP] = "error-drop",
		  [MP_NEXT_LOOKUP4] = "ip4-lookup",
		  [MP_NEXT_LOOKUP6] = "ip6-lookup" },
};

static clib_error_t *
mp_remote_cli (vlib_main_t *vm, unformat_input_t *input, vlib_cli_command_t *cmd)
{
  u32 sw = ~0;
  ip46_address_t remote = {};
  u8 mac[6] = {}, bum = 0, add = 1;
  while (unformat_check_input (input) != UNFORMAT_END_OF_INPUT)
    {
      if (unformat (input, "%U", unformat_vnet_sw_interface, vnet_get_main (), &sw))
	;
      else if (unformat (input, "remote %U", unformat_ip46_address, &remote, IP46_TYPE_ANY))
	;
      else if (unformat (input, "mac %U", unformat_ethernet_address, mac))
	;
      else if (unformat (input, "bum"))
	bum = 1;
      else if (unformat (input, "del"))
	add = 0;
      else
	return clib_error_return (0, "unknown input %U", format_unformat_error, input);
    }
  int rv = vxlan_mp_remote_update (sw, mac, remote, bum, add);
  return rv ? clib_error_return (0, "VXLAN remote update failed: %d", rv) : 0;
}
VLIB_CLI_COMMAND (vxlan_mp_remote_command, static) = {
  .path = "set vxlan remote",
  .short_help = "set vxlan remote <interface> remote <IPv4|IPv6> {mac <MAC>|bum} [del]",
  .function = mp_remote_cli,
};
