.. _sdn_agent_user:

SDN Agent：VPP + FRR EVPN 使用指南
==================================

本 fork 包含 SDN Agent 的原生多点 VXLAN 和本地适配器。FRR 负责 BGP/EVPN，
VPP 负责租户二层和三层转发。已验收基线是本地 VPP 26.06 与 FRR 10.7.0；
适配代码推送后的 GitHub 完整镜像构建尚待验证，不应把本文当作生产性能认证。

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

构建时选择版本
--------------

在 Linux Docker 宿主机、sdn-agent 仓库内执行。VPP_REF 必须包含本 fork 的适配代码，
FRR_REF 默认 frr-10.7.0；两者支持 tag、分支或 commit，发布时优先使用完整 commit。
选择别的版本只代表选择源码，不保证其 API、依赖或 Alpine 编译兼容。

.. code-block:: bash

   cd ~/program/github/sdn-agent
   # 推送适配代码后，替换成真实 ref。当前不宣称该远端流程已完整编译通过。
   VPP_REF='替换为包含适配代码的tag或commit' \
   FRR_REF=frr-10.7.0 BUILD_BASES=1 sh docker/build-vpp.sh

   # 尚未提交的本地代码可以直接构建，不会自动 checkout 本地仓库。
   VPP_SOURCE=/home/alexan/program/subversion/vpp \
   FRR_SOURCE=/home/alexan/program/subversion/frr \
   BUILD_BASES=1 sh docker/build-vpp.sh

默认基础镜像为 alpine/vpp:sdn-base、alpine/frr:sdn-base，集成为
alpine/sdn-agent:vpp-dev；可分别通过 VPP_IMAGE、FRR_IMAGE、AGENT_IMAGE 改名。
这些名字不是源码版本选择器。VPP_REPOSITORY / FRR_REPOSITORY 可替换默认仓库。
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
