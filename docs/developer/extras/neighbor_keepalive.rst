.. _sdn_neighbor_keepalive:

ARP/ND 邻居保活架构
==============================

本文说明本 fork 的 ``neighbor_keepalive`` 插件及 SDN Agent 适配实现。
功能为默认关闭的节点级开关，仅 VPP 数据面支持；整体 EVPN 架构见
:doc:`sdn_agent`，镜像构建与启动见 :doc:`/usecases/sdn_agent`。

.. contents:: 本页导航
   :local:
   :depth: 2

组件与线程分工
------------------------------

.. code-block:: text

   Web / Controller：候选、预览、提交
                    |
   Agent VPP Driver：验证、持久化 Undo、执行、读回、回滚
                    | Unix RPC
   keepalive.py：节点开关、BVI/接入口范围、状态查询
                    | VPP Binary API
   neighbor_keepalive_plugin.so
                    | 主线程 process，每轮约 1 秒
   扫描本地动态邻居 -> 判断到期 -> nk_send()
                    | 单播 ARP Request / IPv6 NS
                本地接入主机
                    | ARP Reply / NA
   接收节点 -> L2 学习 -> 原生 ARP/ND 校验与学习
                    | 主线程 RPC
   邻居更新时间 -> 保活进程确认 -> 下次探测

探测调度、报文构造和失败判定均在 VPP 中完成。Python 配置范围并同步控制面，
不承担逐包收发。``neighbor-keepalive-process`` 是 ``VLIB_NODE_TYPE_PROCESS``
节点，在主线程串行维护保活表；工作线程接收应答，经
``ip_neighbor_learn_dp()`` 或 ``ip_neighbor_confirm_dp()`` 投递到主线程更新邻居。

生产容器的 supervisor 会在适配器持续失联时停止整套进程。隔离验收同时暂停
适配器和 supervisor 来验证 VPP 调度独立性，不代表已实现进程故障容错。

探测对象与数据结构
------------------------------

``nk_process()`` 每轮调用 ``ip_neighbor_walk(AF_IP4/AF_IP6)``，由
``nk_learned()`` 筛选已有邻居。邻居必须是 BVI 上的动态项，其 MAC 必须通过
FDB 定位到已授权的本地接入口，且接入口与 BVI 属于同一个 BD。
BVI 是该 BD 的三层网关接口。

授权范围由适配器根据受管 BVI 和本地 LCP 接入口生成。静态邻居、远端 VXLAN
端口及未受管接口不加入；纯 MAC 项和从未产生 IP 邻居学习的主机不参与探测。

.. list-table:: 原生保活对象
   :header-rows: 1
   :widths: 25 75

   * - 对象
     - 内容与用途
   * - ``nk_scope_t``
     - BVI + 接入口，限定可探测的本地链路。
   * - ``nk_target_t``
     - 保存 IP、MAC、BVI、接入口、状态、尝试次数、最近学习时间和下次探测时间。
   * - ``clib_bihash_24_8_t``
     - 按 BVI + 地址族 + IP 查找 pool 中的目标；不同租户/BVI 及不同 IP 分别计时。
   * - 原生邻居与 FDB
     - 邻居更新时间作为存活依据，FDB 确认本地端口归属；保活表不替代转发表。

活动跟踪随接入范围或 MAC 归属变化退出。已跟踪目标的 FDB 暂时老化消失时，
可沿缓存的授权端口继续探测；收到应答后由正常 L2 学习恢复 FDB。

发送与接收路径
------------------------------

``nk_send()`` 从 BVI 选择源地址，复用 VPP 报文模板，构造接入口 Ethernet
rewrite（包括子接口 VLAN 标签），再将报文提交到该接口的输出节点。

.. list-table:: 探测报文
   :header-rows: 1
   :widths: 15 25 30 30

   * - 地址族
     - 报文
     - 源
     - 目标
   * - IPv4
     - 单播 ARP Request
     - BVI IPv4 与 MAC
     - 已知主机 IPv4 与 MAC
   * - IPv6
     - 单播 Neighbor Solicitation
     - BVI IPv6 与 MAC
     - 已知主机单播 IPv6 与 MAC

报文直接从本地接入口发送，不经过 VXLAN BUM 复制。未知地址的首次解析仍由
VPP 原有 ARP/ND 机制处理。

应答正常经过 L2 学习与 ARP/ND 处理：L2 学习刷新 MAC 时间，原生邻居学习更新
``ipn_time_last_updated``。保活进程观察该时间变化后重新计时。发送请求、配置
重放、Linux 永久镜像项以及普通 IP 流量均不构成对应 IP 的存活确认。

IPv6 额外支持无 Target Link-Layer Address（TLLA）选项的 solicited NA。
在通过原有校验后，``ip6_nd.c`` 提取目标 IPv6 和以太网源 MAC，经
``ip_neighbor_confirm_dp()`` 到主线程。``ip_neighbor_confirm()`` 仅刷新相同
接口/IP、MAC 匹配的已有动态邻居；该路径不创建未知邻居、不改变 MAC，
也不刷新静态项。错误源 MAC 或非 solicited 的无选项 NA 不续期。

计时与状态机
------------------------------

.. code-block:: text

   reachable -- 到期并成功发送 --> probing
       ^                            |
       | 有效邻居学习               | 5 秒后仍无确认，继续发送
       +----------------------------+
                                    |
                          已发 3 次，再等待 5 秒
                                    |
                                    v
                                  failed
                                    |
                           新的有效学习后重新加入

发送资源不足、源地址不可用或接口状态异常进入 ``send-error``，记录错误并在
后续轮次重试；未成功交给输出节点的请求不增加探测次数。

有效 MAC 老化时间为 T 秒时，基础间隔为 ``(T - 60) / 2``，再提前最多约 10%
分散探测。VPP FDB 时间戳为分钟粒度，因此预留量化误差及三次探测的等待时间。
启用要求老化时间为 0 或不小于 120 秒；0 表示 MAC 不老化，此时基础间隔为 150 秒。

例如 T=300 秒，首次探测约在最近有效学习后的 108–120 秒发送。假定该目标
本轮选中 120 秒，且无调度延迟：

.. code-block:: text

   t=0       有效 ARP/ND 学习
   t=120     第一次探测
   t=125     无确认，第二次探测
   t=130     无确认，第三次探测
   t=135     仍无确认，进入 failed

任何一次有效学习都会重新计算时间。以上为正常发送情况下的示例，实际时间还
受约一秒的扫描周期、发送预算和发送错误影响。当前最多跟踪 4096 个绑定，每轮
发送预算为 512 个；失败记录最多保留 300 秒供查询，之后释放。

失败撤销与 EVPN 联动
------------------------------

``nk_process()`` 判定失败后调用 ``ip_neighbor_del()`` 删除该动态 IP 邻居。
适配器周期读取失败集合，禁止残留绑定重新发布，并撤销自己拥有的 Linux 镜像：

.. code-block:: text

   VPP 删除失败 IP 绑定
       -> irb.py / sync_neighbors()
       -> Linux 镜像邻居：NUD_FAILED -> 删除
       -> Zebra 撤销该 IP 的本地 EVPN 绑定
       -> bgpd 撤销对应 MAC/IP Type-2
       -> 远端 Leaf 撤销相应主机路由

Linux 镜像使用永久邻居。FRR 对永久项的直接删除有 RFC 5549 特殊处理，先发送
失败状态才能正常触发该 IP 的 EVPN 撤销。内核项已提前清理时，适配器仍使用
发布记录完成通知；发现新 MAC 或远端所有者时避让新项。同步 bridge MAC 也只在
实际值变化时执行，避免重复设置相同 MAC 导致 Linux 清空邻居。

例如同一 MAC 同时绑定 ``192.168.20.12`` 和 ``2001:db8:20::12``：IPv4 无应答、
IPv6 仍应答时，仅删除 IPv4 邻居和对应 Type-2；IPv6 绑定和共享 MAC 保留。
插件不直接删除共享 FDB 项，MAC 继续正常学习和老化。主机恢复有效学习后重新
发布绑定；已撤销的失败目标不持续主动探测。健康绑定无需周期性重发 Type-2。

配置、接口与代码入口
------------------------------

节点开关使用 ``NEIGHBOR_KEEPALIVE / SET``，显式 ``enabled=false`` 表示关闭。
开关经 Agent 事务保存到适配器 version 3 状态，旧 version 1/2 默认关闭。
关闭清除保活跟踪状态，健康邻居和 MAC 继续正常老化。VPP 重启恢复开关后，
动态邻居仍需重新学习。

.. list-table:: 源码定位（相对 VPP 仓库）
   :header-rows: 1
   :widths: 40 60

   * - 文件 / 函数
     - 职责
   * - ``src/plugins/neighbor_keepalive/neighbor_keepalive.c``
     - ``nk_process`` 调度；``nk_learned`` 纳管与确认；``nk_send`` 发包；``nk_show`` 诊断。
   * - ``src/plugins/neighbor_keepalive/neighbor_keepalive.api``
     - ``set`` / ``status``、``scope_add_del`` / ``scope_dump``、``dump``；均以 ``neighbor_keepalive_`` 为前缀。
   * - ``src/vnet/ip6-nd/ip6_nd.c``
     - 无 TLLA solicited NA 的确认入口。
   * - ``src/vnet/ip-neighbor/ip_neighbor_dp.c`` 与 ``ip_neighbor.c``
     - 工作线程到主线程 RPC、已有动态邻居的匹配与刷新。
   * - ``extras/sdn-agent/keepalive.py``
     - ``reconcile`` 配置开关与接入范围，``failed`` / ``query`` 读取状态。
   * - ``extras/sdn-agent/irb.py``
     - ``sync_neighbors`` 控制本地绑定发布与撤销。

.. code-block:: bash

   vppctl show neighbor-keepalive

查询展示开关、目标数、探测/确认/失败次数、发送及容量错误，以及每个 IP 的状态。
Agent 的 ``NEIGHBOR_KEEPALIVE_STATUS`` 还提供按 VRF、BD、接口和地址族筛选的读回。
当前通过轮询同步状态，没有新增异步订阅接口。

验收入口
------------------------------

VPP 仓库单元测试位于 ``extras/sdn-agent/test_keepalive.py``。配套 Agent 仓库的
``lab/vpp_keepalive.py`` 由 ``SDN_VPP_KEEPALIVE_ONLY=1 python3 lab/vpp_irb.py``
运行；具体镜像参数与验证记录见 Agent 的 ``docs/developer/vpp-keepalive.rst``。
验收覆盖双栈静默保活、单 IP 撤销/恢复、错误 MAC 的 NA、回滚和关闭后的转发。
容器软件端口结果不代表物理 NIC、规模或性能验收。
