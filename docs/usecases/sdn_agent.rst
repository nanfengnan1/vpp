.. _sdn_agent_user:

SDN Agent：VPP + FRR EVPN 使用指南
==================================

本 fork 包含 SDN Agent 的原生多点 VXLAN 和本地适配器。FRR 负责 BGP/EVPN，
VPP 负责租户二层和三层转发。已验收基线是本地 VPP 26.06 与 FRR 10.7.0；
GitHub 分支 sdn/v26.06 的完整镜像构建已验证；具体源码提交与软件验收范围
见配套 Agent 的 docs/developer/vpp.rst，不应把本文当作生产性能认证。

.. contents:: 本页导航
   :local:
   :depth: 2

组件与目录
----------

* 本仓库 src/plugins/vxlan：原生多点 VXLAN、双栈远端表、BUM 和 GSO 软件回退。
* 本仓库 extras/sdn-agent：adapter.py、irb.py 和单元测试。
* 配套仓库 https://github.com/nanfengnan1/sdn-agent：Agent、FRR Driver、
  docker/build-vpp.sh、Dockerfile、事务客户端及实验脚本。
* FRR 源码仓库：https://github.com/nanfengnan1/frr。
* 架构和节点说明见 :doc:`/developer/extras/sdn_agent`，基础 LCP 见
  :doc:`/developer/plugins/lcp`。Agent 的完整操作码与启动参数见其
  docs/user/vpp.rst、docs/developer/vpp.rst。

.. _sdn_vxlan_user_modes:

与原版 VXLAN 的使用差异
----------------------------------------

原版 VPP 已支持 IPv4/IPv6 VXLAN 和 group 组播隧道。本 fork 新增源地址型多点
Ethernet VXLAN：在同一本地 VTEP、Underlay FIB、端口上下文中，一个 VNI 接口
承载多个远端。封装格式仍是标准 VXLAN，详细对比和架构见
:ref:`sdn_vxlan_comparison`、:ref:`sdn_vxlan_native_pipeline`。

.. list-table:: 三种接口使用方式
   :header-rows: 1
   :widths: 20 40 40

   * - 模式
     - 外层目的与复制方式
     - 当前 Agent 支持情况
   * - 原有固定 dst
     - 一个单播隧道接口指向固定远端；多个远端可建立多个接口。
     - VPP 原生能力保留；本适配器的受管 VXLAN 使用多点模型。
   * - 原有 group
     - 外层发往组播地址，由 Underlay 组播网络复制。
     - VPP 原生能力保留；Agent 尚无 group 配置、同步和回滚支持。
   * - 新增源地址型多点
     - 内层目的 MAC 决定远端；BUM 在本地按成员列表做单播入口复制。
     - 当前受管方式，由 FRR 有效状态驱动 MAC→VTEP 和 BUM 成员更新。

以下是原生 CLI 语义对照，供独立测试环境使用，不是完整的组网配置。
不要用它们修改已经由 Agent 管理的同一对象。GigabitEthernet0/8/0 仅为接口名示例。

.. code-block:: text

   固定远端：
   create vxlan tunnel src 10.255.0.11 dst 10.255.0.12 vni 10010

   Underlay 组播：
   create vxlan tunnel src 10.255.0.11 group 239.1.1.1 GigabitEthernet0/8/0 vni 10010

   本 fork 的多点接口：
   create vxlan tunnel src 10.255.0.11 vni 10010
   set vxlan remote <创建返回的接口名> remote 10.255.0.12 mac 02:00:00:10:00:12
   set vxlan remote <创建返回的接口名> remote 10.255.0.12 bum

手动添加远端映射并不代替 BD 成员绑定、BD FDB 和 Underlay 可达性配置。
在 Agent 模式中，这些相关状态由配置操作和适配器协调。VPP 接口名、逻辑名称、
sw_if_index 和 VNI 是不同标识，查询结果会给出它们的对应关系。

当前学习和普通业务环境无需启用 group。即使租户业务包含广播或组播，也可以通过
入口复制传输；只有实测的 BUM 复制开销或明确的组播网络需求，才值得扩展该模式。
创建 group 隧道本身不会配置整个 Underlay 的组播成员和转发，决策依据见
:ref:`sdn_vxlan_bum_modes`。

多点接口可以先于 EVPN 远端出现，最后一个远端撤销后接口仍保留。
Type-2 单播映射和 Type-3 BUM 成员独立管理；跨网段采用 Ethernet L3VNI 和
Router MAC，对称 IRB 的基础 BD/BVI/FIB 能力并非本次才引入。

构建时选择版本
--------------

在 Linux Docker 宿主机、sdn-agent 仓库内执行。VPP_REF 默认 sdn/v26.06，
FRR_REF 默认 frr-10.7.0，分别来自 nanfengnan1/vpp 和 nanfengnan1/frr。
两者支持 tag、分支或 commit，发布时优先使用完整 commit。
选择别的版本只代表选择源码，不保证其 API、依赖或 Alpine 编译兼容。

.. code-block:: bash

   cd ~/program/github/sdn-agent
   # 使用 GitHub 默认分支和 FRR tag；清除本地源码覆盖。
   unset VPP_SOURCE FRR_SOURCE
   BUILD_BASES=1 sh docker/build-vpp.sh

   # 分支有新提交时刷新源码缓存，也可把 VPP_REF 固定为完整 commit。
   VPP_REF=sdn/v26.06 SOURCE_CACHE_BUST="$(date +%s)" \
   BUILD_BASES=1 sh docker/build-vpp.sh

   # 尚未提交的本地代码可以直接构建，不会自动 checkout 本地仓库。
   VPP_SOURCE=/home/alexan/program/subversion/vpp \
   FRR_SOURCE=/home/alexan/program/subversion/frr \
   BUILD_BASES=1 sh docker/build-vpp.sh

默认基础镜像为 alpine/vpp:sdn-base、alpine/frr:sdn-base，集成为
alpine/sdn-agent:vpp-dev；可分别通过 VPP_IMAGE、FRR_IMAGE、AGENT_IMAGE 改名。
这些名字不是源码版本选择器。VPP_REPOSITORY / FRR_REPOSITORY 可替换默认仓库。
上游 v26.06 不含本 fork 的多点 API，不能直接替代 sdn/v26.06。
显式设置 VPP_SOURCE / FRR_SOURCE 时优先使用对应本地源码，不会拉取该组件的 Git ref。
BUILD_BASES=0 复用新流程生成的基础镜像，旧镜像缺少内置适配器时需要先重建。
本地源码路径须为含 .git 目录的完整 clone；脚本单独传递 Git 上下文，
避免 FRR 的 .dockerignore 排除版本历史。linked worktree 暂不支持。
移动分支可能命中旧 Docker 缓存，更新 SOURCE_CACHE_BUST 或改用新 commit。

VPP 原生插件、API JSON、vpp_papi 与适配器来自同一份源码、同一个基础镜像。
镜像内 /usr/share/vpp/source.json、/usr/share/frr/source.json 保存实际 commit、
请求 ref 和本地修改标记。构建脚本不发布镜像、不提交代码、不切换数据面。

节点启动和接口
--------------

Agent 的 agent.dataplane=vpp 选择 VPP；agent.frr=true。配置切换需重启，
不同数据面不能复用同一份持久化状态。集成镜像运行 Agent、Redis、FRR、VPP 和适配器。
默认 startup.conf 使用软件接口，不启用 DPDK，也不会接管物理 NIC。
物理 VF 模式需显式传入独占 VFIO group、hugepages 和经过检查的 DPDK startup.conf。

先建立实际接口和 LCP pair，再下发租户/BD/VXLAN 操作。以下是单独测试接口示例：

.. code-block:: text

   set interface state vf0 up
   lcp create vf0 host-if uplink
   lcp lcp-sync on
   show lcp

.. code-block:: bash

   # 在对应节点的 Linux namespace，非宿主机管理接口
   ip link set uplink up
   ip addr add 10.0.1.2/30 dev uplink
   vppctl show interface address
   vppctl show ip fib

Linux uplink 通过 TAP 与 VPP 配对接口交换控制报文，Netlink 同步地址、邻居和
普通 IP 路由。业务 BD/VRF/VXLAN 的控制镜像由适配器管理，普通 LCP 不会自动
把所有 VPP 对象暴露给 FRR。镜像 tc 隔离不能移除，否则可能产生 Linux 转发旁路。

跨网段示例
----------

.. list-table:: TENANT-A，table1001，L3VNI50000
   :header-rows: 1

   * - 设备
     - PC / 网关
     - 本地业务域
   * - Leaf1
     - 192.168.10.11/24 / 192.168.10.254
     - br10 / L2VNI10010
   * - Leaf2
     - 192.168.20.12/24 / 192.168.20.254
     - br20 / L2VNI10020

分别建立业务 BD/BVI 与传输 BD(br50000)/无编号 BVI，绑定同一租户。
传输 BVI 配置各自 Router MAC；VXLAN50000 加入传输 BD。配置 FRR 的 L3VNI
和共同 L3 RT65000:1，本地业务 L2 RT 则分别为 65000:10010 / 65000:10020。
先 ping 本地网关触发主机学习，再检查远端 Type-2 和租户 /32 主机路由。

.. code-block:: text

   PC1 -> access -> br10 -> 网关 BVI -> tenant FIB
       -> 传输 BVI -> br50000 -> Router MAC 查 FDB
       -> VXLAN50000 -> underlay -> Spine -> 远端 VTEP
       -> 解封装 -> br50000 -> 本机 Router MAC / 传输 BVI
       -> tenant FIB -> 网关 BVI -> br20 -> access -> PC2

Spine 只转发外层 IP，不解封装 VXLAN。跨网段业务使用 L3VNI50000；双方无需
创建对端的业务 BD。Type-2 主机路由足以完成此流程，发布整个前缀才另行启用 Type-5。
每个 VNI 的原生多点接口只配置本地 src；封装 dst 从远端映射动态取得。

查询、验收与边界
----------------

.. code-block:: bash

   docker exec sdn-vpp-node sdnctl -insecure -kind VXLAN_TUNNELS query
   docker exec sdn-vpp-node sdnctl -insecure -kind ROUTES -vrf TENANT-A query
   docker exec sdn-vpp-node vtysh -c 'show bgp l2vpn evpn route'
   docker exec sdn-vpp-node vppctl show lcp
   docker exec sdn-vpp-node vppctl show l2fib
   docker exec sdn-vpp-node vppctl show ip fib table 1001
   docker exec sdn-vpp-node vppctl show ip6 fib table 1001
   docker exec sdn-vpp-node vppctl show vxlan tunnel

Agent 仓库 lab/vpp_evpn.py、vpp_irb.py、vpp_offload.py 覆盖软件二层、对称 IRB、
Type-5、双栈、GSO 与回滚。lab/vpp_sriov.py 另覆盖 Huawei VF 的 Spine/Leaf/PC：
Leaf–Spine 使用三只真实 VF 和两个 VLAN，PC 使用 VPP TAP，Docker 网络只用于管理。
IPv4/IPv6 VTEP 分别通过 30/31 项检查，Spine 断链会中断业务，浏览器查询通过 6 项。
这是同一 NIC 内部 VF 交换，尚未验证外部物理布线、硬件 VXLAN TSO、RX LRO、RSS
多队列或生产性能。物理测试使用静态 underlay / 接口 VTEP；OSPF+Loopback 参考组网
尚未在该 VF 实验中验收。Type-5 在软件实验中验收，未扩大为物理 VF 的验收结论。

underlay 和 LCP pair 由启动配置重建；当前未实现全量冷启动 Running 重放。
租户协议 punt、多归属、HA 和在线切换数据面也未实现。完整恢复边界以 Agent 文档为准。

本地动态邻居保活
----------------

src/plugins/neighbor_keepalive 提供默认关闭的原生插件。
Agent 通过 extras/sdn-agent/keepalive.py 配置节点开关与 BVI/本地接入口范围；
neighbor-keepalive-process 在主线程独立维护已学习到的动态 IP 邻居。
已有 ARP/ND 校验与学习路径提供确认时间，应答仍正常经过 L2 学习。
不拦截后丢包，不通过探测发送、Linux 永久项或周期 BGP 通告更新存活状态。

键为 BVI + 地址族 + IP，记录 MAC 与接入端口。向已知主机发单播 ARP Request
或 IPv6 NS；三次探测间隔 5 秒，无应答删除对应动态邻居。
同 MAC 的多个 IPv4/IPv6 地址独立维护，远端静态邻居、VXLAN 端口、未受管接口
不加入范围。失败项最多保留 300 秒供查询，新有效学习可重新激活。
当前通过周期查询同步结果，没有新增异步事件订阅。

VPP FDB 为分钟粒度，探测间隔 (age-60)/2 并提前最多 10% 抖动。
启用要求 age=0 或 >=120 秒；0 时采用约 135–150 秒。
最多跟踪 4096 绑定，每秒最多 512 探测，发送及容量错误通过 API 公开。
状态模型 version 3 持久化开关，兼容旧 version 1/2；动态存活结果不作为冷启动依据。

在 plugins 配置中启用 neighbor_keepalive_plugin.so，然后通过 Agent
NEIGHBOR_KEEPALIVE SET 设置 enabled=true。NEIGHBOR_KEEPALIVE_STATUS 提供查询。
原生 API 为 neighbor_keepalive_set、neighbor_keepalive_status、
neighbor_keepalive_scope_add_del、neighbor_keepalive_scope_dump 和 neighbor_keepalive_dump。

集成实验位于 sdn-agent/lab/vpp_keepalive.py，由 vpp_irb.py 在
SDN_VPP_KEEPALIVE=1 时执行；可结合 SDN_VPP_STACK=1 验证 Web/Gateway/Controller
候选提交。SDN_VPP_UNDERLAY=6 测试 IPv6 VTEP，内层一直是双栈。
实验输出决定验收结论，不代表物理 NIC 或规模性能测试通过。
