.. _sdn_agent_architecture:

SDN Agent 适配架构与开发
========================

本文描述本 fork 的 EVPN / 对称 IRB 实现。使用与构建入口见
:doc:`/usecases/sdn_agent`；配套 Agent 仓库的 docs/developer/vpp.rst
提供完整操作、节点表、参考组网与硬件验收步骤。

.. contents:: 本页导航
   :local:
   :depth: 3

.. _sdn_vxlan_comparison:

原版 VXLAN 与当前实现的差异
----------------------------------------

对比基线为本仓库 VPP 26.06 的 ``c3200b88d``；适配实现首次提交为
``2814bd686``。这里的“原版”指该基线的 VXLAN 插件，不代表 VPP 其他插件或
外部控制器不能组合出 EVPN/IRB 功能。原版已经支持 IPv4/IPv6 外层、固定远端
和组播隧道；本次增加源地址型多点 Ethernet VXLAN 及 SDN Agent 适配链路。
线上仍是标准 VXLAN 封装，新增接口模型没有增加私有报文字段。

.. list-table:: 配置与转发模型对比
   :header-rows: 1
   :widths: 18 38 44

   * - 项目
     - 原版固定远端 / 组播模式
     - 当前新增多点模式与适配器
   * - 接口模型
     - 单播接口对应固定本地 VTEP、远端 VTEP、VNI 等参数；组播模式另配置 group。
     - 同一本地 VTEP、Underlay FIB 和 UDP 端口上下文中，每个 VNI 一个多点接口。
   * - 创建条件
     - CLI 要求 src 和 dst，或 src 和 group/接口。
     - 只提供 src 和 VNI 即可建立接口；Binary API 的 dst 使用对应地址族的未指定地址。
   * - 单播远端选择
     - BD 的 FDB 选择隧道接口，接口的固定 dst 决定外层目的 IP。
     - BD 的 FDB 先选择多点接口，再由接口内部的 MAC→VTEP 表选择外层目的 IP。
   * - BUM 复制
     - BD 可向多个单播隧道复制；也可通过组播隧道交给 Underlay 复制。
     - 多点接口内部维护 BUM 成员列表，向各远端做单播入口复制。
   * - 远端生命周期
     - 外部控制程序维护对应的隧道接口和 FDB。
     - 添加、移动、撤销远端主要更新表项；远端列表为空时仍保留接口。
   * - IPv4/IPv6
     - 两种外层地址族均已支持。
     - 新的多点接口及 v2 远端 API 也支持双栈；远端地址族必须与本地 VTEP 一致。
   * - Underlay 转发
     - 使用固定封装头及跟踪远端路由、重栈的 next_dpo。
     - 选择远端后构造封装头，设置 encap_fib_index，进入 ip4-lookup/ip6-lookup。
   * - 接收查找
     - 按配置的远端及本地端点、VNI、UDP 端口和 Underlay FIB 匹配。
     - 保留固定隧道优先级；未命中时再按本地 VTEP、VNI、端口/FIB 查多点接口。
   * - EVPN 控制面
     - VXLAN 插件本身不运行 BGP，也不负责消费 FRR 的 EVPN 路由。
     - 新增适配器消费 Zebra 安装到 Linux 的有效状态；BGP/EVPN 选路仍由 FRR 完成。
   * - 对称 IRB
     - VPP 已有 BD、BVI、FIB 等基础对象，需要外部配置组合。
     - irb.py 组织租户 FIB、业务/传输 BD/BVI、Router MAC 和 L3VNI 相关路由。
   * - GSO 与校验和
     - 原有封装节点已有软件校验和及卸载标记处理。
     - 多点路径增加 TCP GSO 软件分段和校验和补全；解封装独立校验外层 UDP。

同一个 VNI 有两个远端时，接口和转发表组织如下。vx10010 是逻辑名称示例，
实际 VPP 接口名和 sw_if_index 以查询结果为准。

.. code-block:: text

   固定远端单播模式：本地 10.255.0.11，VNI 10010

   br10 -- vxlan_tunnel0 -- dst 10.255.0.12
        \- vxlan_tunnel1 -- dst 10.255.0.13

   BD FDB: MAC-A -> vxlan_tunnel0
           MAC-B -> vxlan_tunnel1

   新增多点模式：本地 10.255.0.11，VNI 10010

   br10 -- vx10010 -- MAC-A -> 10.255.0.12
                  |  MAC-B -> 10.255.0.13
                  \- BUM   -> [10.255.0.12, 10.255.0.13]

   BD FDB: MAC-A -> vx10010
           MAC-B -> vx10010

VNI 不是整个进程内唯一的接口身份，必须结合本地端点、端口和 Underlay 上下文。
每个发出的报文仍有明确的外层目的 IP；未指定 dst 是创建时的本地模型，不会把
0.0.0.0 或 :: 当成线上目的地址。旧固定 dst 与 group 模式继续保留。
新增多点模式仅承载 Ethernet，不支持 raw-IP ``is_l3``。

控制通道与资源归属
------------------

.. code-block:: text

   Web -> Controller -> gRPC -> SDN Agent
                                  |
                   +--------------+---------------+
                   |                              |
             VPP composite                    FRR Driver
               |       |                          | vtysh
               |       +-- Linux Driver/Undo       v
               |                |                bgpd
               |                v                 | ZAPI
               |         Linux control mirrors <- Zebra
               |         VRF/bridge/VXLAN/FDB      Netlink
               |         neighbor/tenant routes
               |                ^   |
          Unix HTTP/JSON        |   | selected state
               |        local   |   v
               +------> Python adapter (adapter.py + irb.py)
                        |           ^
             Binary API |           | native readback
                        v           |
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
Python 是常驻控制进程，不参与逐包封装和复制。Agent 到适配器的 Unix socket
默认是 /run/sdn-agent/vpp.sock；适配器用 vpp_papi 连接 /run/vpp/api.sock。
配置提交、状态同步与业务转发分别走上述控制通道和下文的数据面路径。

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

.. _sdn_vxlan_native_pipeline:

多点接口的逐包处理与状态更新
----------------------------------------

``vxlan_tunnel_t`` 保留公共隧道参数，新增 ``multipoint`` 标志、
``remote_by_mac`` 哈希、``remote_ips`` 地址池和 ``flood_remotes`` 列表。
MAC 哈希的值是地址池索引，BUM 列表独立维护。``vxlan_mp_remote_update()``
负责增删远端，``vxlan-mp-encap`` 是实际执行封装的 INTERNAL 节点。

单播、BUM 与解封装
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: text

   同 BD 单播：
   access -> l2-input / BD FDB -> 多点 VXLAN 接口
          -> vxlan-mp-encap -> 内层目的 MAC 查 remote_by_mac
          -> 构造外层 IP/UDP/VXLAN -> underlay ip4/ip6-lookup -> 出口

   BUM 或未知单播（BD 允许泛洪时）：
   access -> BD flood -> 多点 VXLAN 接口 -> vxlan-mp-encap
          -> flood_remotes
               +-> 副本 1，外层 dst=远端 VTEP 1 -> underlay lookup
               \-> 副本 2，外层 dst=远端 VTEP 2 -> underlay lookup

   接收：
   underlay -> VXLAN 输入路径 -> vxlan4-input / vxlan6-input
            -> 校验外层 UDP、查找固定隧道或多点接口
            -> 解封装 -> l2-input / 该接口所属 BD

实际接收前置节点取决于设备插件、UDP 分发和 vxlan-bypass 配置，以上为逻辑路径。
在多点封装节点内，已知单播命中 MAC 映射后只发给该远端；未命中或目的 MAC 为
组播/广播时使用 BUM 列表。需要泛洪而列表为空时，报文计入 no remote VTEP 并丢弃。
这里的 Underlay FIB 查找与租户 FIB 不同：同 BD 通信不需要先查租户路由，
跨网段则先按前文的对称 IRB 路径完成租户路由和 Router MAC 选择。

接收侧的多点查找不要求源 VTEP 已存在于发送远端表，MAC→VTEP/BUM 表不构成接收 ACL。
外层 UDP 校验位于公共 vxlan_input 路径，旧固定远端隧道经过该路径时也会执行，
因此本次接收侧修正不只影响新增多点接口。
适配器给多点桥端口设置 SHG 1 并关闭该端口的数据面 MAC 学习，避免远端间反射和
远端条目覆盖；这些策略由适配器设置，不能只创建一个裸 VXLAN 接口就假定已经具备。

远端变化与兼容边界
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* Type-2 驱动单播映射，Type-3 驱动 BUM 成员，撤销其中一种不会隐式删除另一种。
* 同一 MAC 移动到新 VTEP 时更新地址池；删除请求同时比较远端地址，旧目的的迟到
  撤销不会删除已迁移的新映射。更新位于 worker barrier 内。
* ``vxlan_mp_remote_add_del_v2`` / ``vxlan_mp_remote_v2_dump`` 支持双栈，
  IPv4-only v1 API 继续保留。VXLAN API 版本为 2.3.0。
* L2 API 3.4.0 的 ``l2_egress_flood`` 调整向桥成员复制的类别，不能与接口
  ingress FLOOD 开关混为一谈；多点接口内部的远端列表由 VXLAN API 管理。
* TCP GSO 在多点封装前软件分段；有 IPv6 扩展头时修正传输偏移、长度和校验和。
  IPv6 外层 UDP 校验和必须有效。硬件隧道 TSO/LRO 不属于这次能力承诺。

多点方式减少了随远端数量增长的接口对象，但增加了封装阶段的 MAC 查表和动态
封装处理，BUM 复制仍消耗发送端 CPU、buffer 和出口带宽。未完成对照性能测试前，
不能从接口数量减少推导吞吐提高，也不能将软件回退等同硬件卸载。

.. _sdn_vxlan_bum_modes:

group 与入口复制的取舍
----------------------------------------

原生 ``group`` 指定的是外层目的组播 IP 和承载接口，不是 EVPN 邻居组，也不是
一个 VTEP 地址列表。组播隧道让发送端发出一份 BUM 封装报文，由 Underlay 复制；
当前多点模式根据 EVPN Type-3 成员，在入口 VPP 生成逐远端的单播副本。

.. code-block:: text

   group 模式：
   Leaf1 -- 外层 dst=239.1.1.1 --> Underlay 组播树 --+-> Leaf2
                                                   \-> Leaf3

   当前入口复制：
   Leaf1 -- 副本 1，dst=10.255.0.12 --> Underlay 单播路由 --> Leaf2
         \- 副本 2，dst=10.255.0.13 --> Underlay 单播路由 --> Leaf3

组播模式依赖正确的组播转发和接收成员配置；单独创建 VPP group 隧道不会替网络
建立完整的 PIM/IGMP/MLD 或静态组播状态。当前 Agent 的 VXLAN typed operation
没有 group 字段，受管路径采用入口复制；保留 VPP 原生 CLI 不等于 Agent 已支持
组播隧道的创建、核对和回滚。租户内有组播业务也不自动要求外层使用 group。

现阶段继续采用入口复制，按“每个 VNI 的远端数 × BUM 流量”评估复制开销。
当实测表明发送端复制/带宽成为瓶颈，或部署明确要求组播 Underlay 时，再扩展
组播组分配、EVPN 隧道属性映射、成员生命周期和故障/重复报文验收。
入口复制和组播树都是 EVPN 支持的方式，见
`RFC 8365 第 9 节 <https://www.rfc-editor.org/rfc/rfc8365.html#section-9>`_。

源码和运行时的一致性
--------------------

Agent 构建的 VPP 基础镜像同时包含 native libraries、API JSON、vpp_papi、adapter.py、
irb.py。集成镜像从同一 VPP 镜像复制它们，禁止另外叠加不同 checkout 的 Python 文件。
FRR 从 nanfengnan1/frr 的指定 ref 构建；source.json 记录实际 commit 和工作区修改。
Git ref 可选择不代表任意版本兼容，源码预检查会拒绝缺少本 fork API 的上游版本。
远端完整源码镜像构建仍待验证；本地代码提交和功能验收不等同指定远端 ref 构建或发布成功。

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
