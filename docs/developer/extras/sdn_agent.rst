.. _sdn_agent_architecture:

SDN Agent 适配架构与开发
========================

本文描述本 fork 的 EVPN / 对称 IRB 实现。使用与构建入口见
:doc:`/usecases/sdn_agent`；配套 Agent 仓库的 docs/developer/vpp.rst
提供完整操作、节点表、参考组网与硬件验收步骤。

.. contents:: 本页导航
   :local:
   :depth: 3

控制通道与资源归属
------------------

.. code-block:: text

   Web -> Controller -> gRPC -> SDN Agent
                                |             |
                         VPP composite     FRR Driver
                                |             | vtysh
                         Unix HTTP          bgpd
                                |             | ZAPI
                        Python adapter      Zebra
                          |       ^           | Netlink
                 Binary API       +-- Linux control mirrors
                          |             VRF / bridge / VXLAN / neighbor
                          v
                VPP tenant FIB / BD / BVI / FDB / VXLAN

   Underlay Linux IP state -> Netlink -> linux_nl -> VPP ordinary IP objects
   Linux protocol packets <-> TAP/TUN + LCP punt/xc <-> VPP actual interfaces

FRR 负责 BGP 最优路由与 EVPN 语义，Zebra 通过内核对象观察 VNI、VRF、SVI、MAC/IP。
没有单独的 FPM server，也没有改造 Zebra 为直接编程 VPP；当前适配器读取 Zebra
安装到受管镜像的有效 FDB、邻居和租户路由，再调用 Binary API。
普通 linux_nl 只解决受支持的接口/IP 状态同步，不能代替 EVPN 的 VNI、Router MAC、
BUM 和租户业务域管理。Linux 镜像由 Agent 创建，不能将任意 TAP 伪装成 VXLAN。

Agent composite 内部复用 Linux Undo，不能再独立注册第二个 Linux Driver。
运行模型为 tenant/bridge/binding/vxlan/l3vni 等对象，BD ID 与 VNI/table ID 分开。
对象使用 sdn-agent/ ownership tag，拒绝抢占别的组件创建的资源。
Linux 镜像 tc 双向隔离业务，underlay LCP 通道保留；VPP BVI 网关业务不经 Linux 转发。

LCP 与图节点
------------

配对身份为 phy_sw_if_index（真实 VPP 端口）、host_sw_if_index（VPP TAP/TUN 端）、
Linux host_if_index（目标 namespace 中 netdev）。phy 也可以指软件接口。
src/plugins/linux-cp/lcp_interface.c 创建 pair、启用 feature 并安装 punt redirect。
src/plugins/linux-cp/lcp_nl.c 的 linux-cp-netlink-process 为 PROCESS 节点，
等待控制事件；下列 lcp_node.c 节点为 INTERNAL，属于逐包处理：

.. list-table:: 主要 LCP 节点
   :header-rows: 1

   * - 节点
     - 职责
   * - linux-cp-xc-ip4 / ip6
     - 主机 TAP → phy；用 Ethernet rewrite 找 adjacency，运行对应输出 feature。
   * - linux-cp-xc-l3-ip4 / ip6
     - 主机 TUN → phy；P2P adjacency 或 P2MP FIB lookup，避免本地报文重复减 TTL。
   * - linux-cp-xc-mpls
     - 主机 MPLS 到配对端口；本项目没有将节点存在视为 MPLS 验收。
   * - linux-cp-punt / linux-cp-punt-xc
     - 按 pair 选择主机或实际接口，TAP 路径恢复 L2 头；不匹配 pair 则不能交付。
   * - linux-cp-punt-l3
     - TUN punt 的 locally-originated/TTL 处理，继续 punt redirect。
   * - linux-cp-arp-phy / linux-cp-arp-host
     - 当前 phy 节点复制 ARP reply 给主机、继续原 feature；host 节点将 ARP 发往 phy。

设备插件的 dpdk-input、tap-input 是 INPUT 节点；interface-output 与实际设备输出
完成发送。Linux 发包从 TAP/vhost 队列进入 tap-input，再经 xc 节点到实际出口；
VPP 选定需 punt 的接收包走配对 host TAP 交 Linux。Netlink 不携带业务帧，
普通转发包也不应先走 Linux。通用 LCP 的 Linux ARP/ND/ping 建议与本适配的
租户 BVI 不完全相同：当前租户网关 ping/邻居在 VPP 处理，租户 BGP/OSPF punt 未实现。

EVPN 到原生对象的转换
----------------------

.. list-table:: 有效状态的消费
   :header-rows: 1

   * - 控制状态
     - VPP 更新
   * - Type-3 / 零 MAC FDB 的远端成员
     - VNI 内独立 BUM 复制列表。
   * - Type-2 MAC、远端 VTEP
     - 业务/传输 BD 的 FDB，以及 VNI 内 MAC→VTEP 映射。
   * - Type-2 MAC/IP 和 Router MAC
     - 租户主机路由、传输 BVI 下一跳邻居、远端 ARP/ND 抑制。
   * - Type-5 选中前缀路由
     - 对应租户 FIB 的前缀/ECMP 下一跳；无效候选不写入。
   * - VPP 接入口 FDB / BVI 邻居学习
     - 发布到 Linux 控制镜像，驱动 Zebra/bgpd 通告本地主机。

本地邻居必须匹配接入 FDB，避免把远端 MAC/IP 再次发布成本地。
远端 MAC 使用可移动动态 FDB 项；Type-3 与单播引用分开撤销。
撤销所有远端成员后保留 VNI 接口。适配器约每秒核对，尚未实现全事件驱动。

BD、BVI、VRF 和 L3VNI 的转发
----------------------------

BD 做二层 MAC 查找；BVI 是从 BD 进入租户 FIB、以及从租户 FIB 返回 BD 的接口。
业务 BVI 带网关地址；L3VNI 的传输 BVI 可无 IP，使用 Router MAC 且启用 IP 接收。
L3VNI 是传输 BD 中的 Ethernet VXLAN 端口，不能使用 raw-IP is_l3 来代替。

.. code-block:: text

   Leaf1: PC1 -> 业务 BD10 -> 网关 BVI -> 租户 FIB
                 -> 传输 BVI -> 传输 BD -> 远端 Router MAC 查 FDB
                 -> VXLAN VNI50000 -> underlay FIB -> VF

   Spine: 外层 IP 路由，不解封装。

   Leaf2: VF -> VXLAN 解封装 -> 传输 BD -> 本机 Router MAC / 传输 BVI
                 -> 租户 FIB -> 网关 BVI -> 本地业务 BD20 -> PC2

PC1=192.168.10.11/24，PC2=192.168.20.12/24，租户 TENANT-A/table1001；
业务 VNI10010/10020 不同、L2 RT 不同，共同使用 L3VNI50000 和 L3 RT65000:1。
Type-2 产生远端主机路由即可完成跨网段转发。实际 VF 抓包确认跨网段使用 VNI50000，
内层 MAC 为两端 Router MAC。br50000 是逻辑名称，不保证数值 BD ID=50000。

原生 VXLAN 的实现位置
----------------------

* src/plugins/vxlan/multipoint.c：vxlan-mp-encap（INTERNAL），按内层目的 MAC
  选择远端；BUM 按成员复制，之后进入 underlay ip4/ip6-lookup。
* vxlan.c / vxlan.h：src-only 多点接口状态、生命周期及旧固定 dst 模式共存。
* vxlan.api / vxlan_api.c：API 2.3.0 的 vxlan_mp_remote_add_del_v2 和
  vxlan_mp_remote_v2_dump 支持双栈；旧 IPv4 API 保留。
* decap.c：vxlan4-input / vxlan6-input 匹配本地地址、VNI、端口/FIB，
  先匹配固定 dst，再匹配多点接口；验证外层 UDP 并清理不适用的 offload 标记。
* src/vnet/l2/l2.api / l2_api.c：l2_egress_flood，保留兼容接口。
* src/plugins/tap/rx_node.c：处理 IPv6 扩展头的传输偏移与 NEEDS_CSUM。

每个 VNI 一个多点接口，创建不要求固定 dst；线上 dst 总是按远端表确定。
远端列表和本地 VTEP 必须同地址族，外层 IPv4/IPv6 都可承载内层双栈。
更新跨 worker barrier，旧远端的迟到删除不能撤销新的 MAC 目的。
TCP GSO 在封装前软件分段；IPv6 外层 UDP 校验和必须有效；不声称硬件隧道 TSO。
旧固定 src/dst 的 RX flow offload 规则不能套到多点接口，RX LRO 保持关闭。

源码和运行时的一致性
--------------------

Agent 构建的 VPP 基础镜像同时包含 native libraries、API JSON、vpp_papi、adapter.py、
irb.py。集成镜像从同一 VPP 镜像复制它们，禁止另外叠加不同 checkout 的 Python 文件。
FRR 从 nanfengnan1/frr 的指定 ref 构建；source.json 记录实际 commit 和工作区修改。
Git ref 可选择不代表任意版本兼容，源码预检查会拒绝缺少本 fork API 的上游版本。
远端完整构建待适配代码提交、推送后验证；当前不要把本地已验收镜像等同远端发布物。

维护变更时同步三个层面：C API/插件、extras/sdn-agent、配套 Agent capabilities/文档。
修改 API 后必须重建 native 与 PAPI，不能只重启 Python。

事务、恢复和验证
----------------

配置 RPC 和核对在单线程串行执行；Unix socket 为 0600。先原子落盘并 fsync 期望
模型/previous，再改 Linux/VPP；Agent 持久 Undo，Apply 后读回 Verify。
中断操作暂停周期核对，等待 Undo 恢复；模型 v2 兼容旧 L2 状态。
冷启动先重建 underlay/LCP，再恢复受管配置和等待 EVPN；全量 Running 重放尚未实现。

.. code-block:: bash

   # VPP 仓库
   python3 -m unittest discover -s extras/sdn-agent -p 'test_*.py'

   # 配套 Agent 仓库
   python3 -m unittest discover -s docker -p '*_test.py'
   python3 lab/vpp_evpn.py
   python3 lab/vpp_irb.py
   SDN_VPP_UNDERLAY=6 python3 lab/vpp_irb.py
   python3 lab/vpp_offload.py

物理 VF 专用脚本会改变指定 PCI 绑定，只能按 Agent 开发文档的准备/恢复步骤执行。
本机已测单 NIC 的三个 HiNIC3 VF、VLAN Spine、PC TAP、双栈 IRB、接收侧抓包和
Web 查询；硬件 VXLAN TSO/RX LRO、RSS 多队列、外部物理链路、HA 和吞吐未验收。
HiNIC3 还修复了 PMD 描述缺失导致的 VLAN 子接口空指针崩溃；这不扩大为所有 PMD 认证。
