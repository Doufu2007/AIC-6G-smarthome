"""Evidence-bound Chinese submission content. No measurements are invented here."""
from pathlib import Path
import json
ROOT = Path(__file__).resolve().parents[3]
NAME = '6G链路自适应家庭AI推理系统'
VERSION = '2026年11月11日'
FUSED = 'SenseHome-6G/evidence/network_compute/20261111_fused/'
MODEL = 'docs/experiments/autodl_20261103_normalized_v2/'
LOCAL = 'docs/experiments/20261111_fused_real/reviewed_20_final_repeat3/'

def para(text): return {'kind':'p','text':text}
def sub(text): return {'kind':'h2','text':text}
def table(headers, rows, widths=None): return {'kind':'table','headers':headers,'rows':rows,'widths':widths}
def page(title, *blocks): return {'title':title,'blocks':list(blocks)}

ABSTRACT = ('家庭智能体调用远程模型时，广域网、模型负载与输出合规共同决定任务能否完成。SenseHome面向存量住宅集成服务，设计设备规则、家庭网关模型和云端模型三档能力，在复杂指令中联合使用WAN/LAN观测及两端模型健康、队列、内存与并发信息，以纯C策略选择后端。统一网关执行动作白名单、故障回退和事件记录，将接口响应、规划完成与模拟状态确认分别计量。融合版r3完成全量构建和审计，58,800条合成回放中，复杂任务时限内合同完成率为联合84.40%、仅网络62.98%、仅算力61.90%；无迟滞为84.52%，不支持迟滞提高完成率。独立历史真实模型实验在20条冻结标签上动作一致17/20，请求中位数290.679毫秒，尚未与r3联测。当前设备为PC模拟状态，网络为合成轨迹或应用层注入，无家庭试点及6G空口实测。本方案以可解释降级与可复核证据为交付基础，后续按真实需求、模型、网络和物理设备逐层验收。')

PLAN = [
page('场景设计方案',sub(NAME),para('系统代号 SenseHome'),para('场景类型  面向实际需求的应用场景设计方案'),para('版本  '+VERSION),sub('作品简介'),para(ABSTRACT),para('本材料采用匿名评审版本，不含所在单位名称、标识和指导教师信息。报名团队名称及编号由参赛者在正式上传前按实际报名信息补充，不以占位身份代表报名完成。')),
page('一 场景概述与需求来源',sub('目标业务'),para('目标使用者是为存量住宅接入灯光、窗帘和环境设备的集成服务商，最终使用者是住户。集成商需要在网络或模型异常时定位故障、保留可执行操作，并清楚解释能力变化。该客户画像是设计定位，尚无具名客户确认、访谈记录或试点证明。'),sub('业务问题'),para('远程模型路径依赖广域网与上游服务。WAN故障时，LAN和本地设备控制可能仍可用；模型可连接时，也可能因排队、内存不足或输出不合规而不能执行。系统因此分别管理链路、服务与动作边界，避免把收到HTTP响应直接计作任务完成。'),table(['问题','设计响应','验收观察'],[['WAN异常但LAN可用','保留网关模型候选和设备规则','路由原因与实际后端'],['模型繁忙或观测过期','检查两端资源与观测年龄','拒选原因和unknown字段'],['复杂或危险动作','整项校验和失败闭锁','动作计划、拒绝原因、状态差异']],[30,66,54]),sub('公开依据与直接证据'),para('ITU-R M.2160-0将AI与通信融合作为IMT-2030使用场景之一，支持本项目的方向定位。现有家居平台已有本地自动化和模型接入能力，本项目聚焦链路与算力共同异常时的路由、降级和复核。公开技术材料不等同于本项目用户需求证明，尚未采集的访谈不写作已完成。')),
page('二 需求指标与证据口径',para('KPI按证据层分别验收。同一指标必须同时给出输入、样本数、完成定义和计时边界，禁止混合合成时延、HTTP墙钟和真实模型接口时延。'),table(['指标','当前核验结果','适用层'],[['时限内完成固定规划合同','joint 84.40%，network-only 62.98%，每臂复杂任务n=5,040','r3合成回放'],['HTTP复杂任务达标','矩阵joint/network-only 80.95%/61.90%；压力组52.98%/52.54%','本机规划桩墙钟'],['模型参考动作一致','17/20，85.0%；请求p50 290.679 ms、p95 522.643 ms','独立AutoDL历史真实推理'],['本地控制与状态确认','开关灯p95最大4.434 ms；只读安防查询1.711 ms；合计160次确认','旧版PC模拟设备'],['动作白名单边界','28项通过，25项拒绝、3项正向控制','隔离PC安全用例'],['目标部署与用户价值','真实家庭、物理动作、成本和6G空口待验收','设计阶段']],[39,84,27]),sub('完成定义'),para('接口成功表示请求收到了约定响应。合成合同完成表示规划桩满足指定合同。真实模型动作一致根据冻结参考动作核对，拒绝原因另列。PC状态确认是模拟JSON设备状态变化。真实物理执行还需设备回执或传感器确认，当前没有该层证据。'),para('原K1/K2为旧网络策略的时延目标，实测未达标；旧正常档405.6 ms、劣化档596.2 ms不归因于r3。后续真实端到端目标需由需求方确认并绑定硬件、模型和网络条件。')),
page('三 总体架构与业务流程',para('设备端规则、模型网关和远程模型是独立部署角色。当前统一网关运行在普通PC，设备由JSON状态模拟；QEMU原型与Dashboard软件通道分别说明。'),table(['部署角色','职责','当前实现与接口'],[['命令入口','接收指令、展示结果与原因','Dashboard /api/command'],['PC网关','观测适配、调用C策略、后端请求、记录事件','Python；健康缓存；policy_cli'],['唯一策略实现','任务约束、候选可用性与代价、恢复迟滞','offload_policy.c，结构化输入输出'],['模型候选','本地模型或远程模型动作规划','/infer与/health自定义HTTP合同'],['设备边界','校验允许动作并确认状态','Python动作白名单；PC JSON状态']],[30,63,57]),sub('一次请求的处理'),para('先识别任务类别。支持的结构化控制和只读安全查询直接使用本地规则，绕过同步网络探测。复杂指令读取WAN/LAN与两端算力快照，交由C策略选择候选；模型计划经过schema与权威白名单检查后执行。后端异常、输出不合规或候选不可用时按边界回退，事件保存原因、耗时、能力标志和状态。'),sub('适配与兼容'),para('策略核心使用纯C。完整网关依赖Python、HTTP与子进程，目标RTOS需适配计时、网络、IPC和设备API。QEMU的C白名单与Dashboard的Python检查是两套实现，不能称作同一条已验证通道。现有设备工具可保留，但每次平台迁移仍需独立验收。')),
page('三 联合路由与AI加6G映射',sub('两段链路与两端算力'),para('WAN和LAN分别建模。复杂请求首先排除模型未就绪、观测过期或资源超限的候选，再比较已知的网络、排队和推理预算。带宽可用时才加入传输估算，未知字段保持unknown。切换裕量与恢复样本数是项目配置，不是6G标准。Python仅适配观测和执行请求，决策以C实现为准。'),table(['融合方向','当前软件机制','未来接入与验证'],[['网络支持AI','链路预算进入模型位置选择','接真实链路遥测，比较尾时延与完成率'],['AI任务支持协同','安全、结构化、复杂任务分级','测减少的远端调用与传输量'],['算力与网络联合','模型健康、队列、内存、并发参与候选检查','接真实资源采集并校准代价'],['运行可追溯','原因码、观测来源、事件ID和错误分层','真实设备回执与访问控制']],[32,60,58]),sub('差异化与技术边界'),para('端云路由已有广泛研究，家居平台也已有本地自动化。本作品的具体交付是家庭两段链路、三档能力、C策略唯一实现及可审计证据流水线。当前未实现6G空口、网络切片、确定性承载或无线资源分配，也没有证明算法最优。6G相关能力是可替换的未来观测和承载接口，不能由软件桩的结果推导网络标准性能。')),
page('四 融合版r3验证',para('r3于2026年11月11日新加坡时间完成全量验证，Zig 0.17.1 cc前端构建，quick_mode=false，记录源码运行期间未变化。归档328个运行文件与原验证目录逐路径SHA256一致。'),table(['验证范围','样本或测试','结果'],[['C网络策略 / 算力策略','70 / 17','全部通过'],['网关 / Dashboard / 部署','139 / 37 / 25项','全部通过'],['合成replay','58,800条','完整矩阵与审计通过'],['并发 / 敏感性','6,144 / 9,360条','本机墙钟 / 12个单因素变体'],['安全 / HTTP网关','28项 / 9,084请求','用例与持久化事件审计通过']],[44,55,51]),sub('复杂任务结果'),table(['策略臂','n','时限内合同完成'],[['双端联合 joint','5,040','84.40%（4,254/5,040）'],['仅网络 network-only','5,040','62.98%'],['仅算力 compute-only','5,040','61.90%'],['无迟滞 no-hysteresis','5,040','84.52%']],[62,23,65]),para('联合策略仍有786条UNRESOLVED_NO_NLU。无迟滞达标率略高，不支持“迟滞提高完成率”。指定轨迹中joint切换93次、无迟滞207次，仅证明该轨迹中的切换计数差异。上述指标来自合成输入和固定规划桩，不能解释为语言理解准确率或用户体验收益。'),para('复核入口 '+FUSED+'README.md；replay/summary.json；validation/verification.json。')),
page('四 HTTP网关与安全闭环',sub('独立HTTP墙钟实验'),para('HTTP实验包含2,940条矩阵请求和6,144条压力请求，共9,084条请求与持久化事件。矩阵复杂任务joint/network-only时限达标80.95%/61.90%；压力组52.98%/52.54%，压力条件下差距很小，不能概括成稳定的大幅收益。'),table(['观察','数量','解释'],[['外层HTTP响应200','9,084','表示接口响应，不表示业务完成'],['合同未完成','679','383条合同未完成及296条注入503'],['下游HTTP_503','296','应用层故障注入，并非实网丢包'],['HTTP文件清单','283项','SHA256匹配']],[49,26,75]),sub('白名单与故障处理'),para('28个安全用例全部通过，其中25项应拒绝输入被拒绝，3项为允许动作正向控制。检查递归扫描嵌套字段；不合规动作整项拒绝，避免执行部分危险计划。超时、畸形JSON、策略进程异常、fallback异常和并发回归留有结构化错误记录。'),para('测试范围内的拒绝证明特定防护有效，不能推导任意prompt injection或真实物理风险已消除。燃气、门禁等未授权动作不执行；真实部署仍须设备联锁、鉴权、TLS、响应大小限制、访问控制与日志脱敏。'),para('复核入口 '+FUSED+'http_gateway/；safety/；validation/。')),
page('四 独立真实模型证据',para('2026年11月3日已完成AutoDL真实模型推理。模型为Qwen2.5-1.5B-Instruct固定revision，vLLM 0.33.0，RTX 4080 SUPER。20条命令各推理一次，few-shot提示与透明动作归一化均保留原始输出。该历史实验尚未与r3联合策略端到端联测。'),table(['指标','核验结果','计量边界'],[['HTTP成功','17/20','三条422保护拒绝单列'],['参考动作一致','17/20（85.0%）','使用冻结参考动作'],['请求时延p50 / p95','290.679 / 522.643 ms','该模型接口请求墙钟'],['预期拒答','3/3另行记录','不并入普通成功率'],['设备执行','未包含','模型动作规划级']],[41,47,62]),sub('保护层与模型输出分别记录'),para('A01/A02产生未提及房间的动作，保护层返回422；A04阳台动作不在白名单，返回422。这三条属于fail-closed保护拒绝，不能改写为成功响应。原始报告eval.draft.json保留；eval.formal.json是标签冻结后对已捕获真实响应的重新标注，并非重新推理。'),sub('本机补充实验入口'),para('本轮新增实验以同一冻结集、轮换策略臂顺序和完整失败记录为要求，结果只有在原始行、模型配置与来源校验完成后才写入正文。新增结果单列日期与模型，不能与历史20条或r3合成样本拼接成更大样本。'),para('复核入口 '+MODEL+'README.md、eval.formal.json、eval.draft.json；docs/deployment/20条动作标签冻结记录_20261103.json。')),
page('四 实施路径与验收里程碑',table(['阶段','交付与验收','当前状态'],[['软件与融合策略','C构建、测试、unknown闭锁、事件、全量实验审计','已完成r3归档'],['真实模型独立验证','固定模型配置、冻结标签、逐条响应、拒绝与时延','已有历史20条'],['本机联合评测','同集四臂、原始失败、端到端计时、资源观测','本轮持续补证'],['需求采集','集成商或住户知情同意、逐题原话、KPI确认','尚无真实回答'],['目标设备与网络','授权设备、真实链路、重复尾时延和回执','未开展'],['试用与推广','至少明确一类目标客户，验证安装工时与维护成本','后续依验收推进']],[32,88,30]),sub('可靠性验收'),para('新增模型和硬件先在隔离设备状态中运行，保留模型原始计划与归一化后的动作。网络故障、模型超时、输出错误和执行错误分别注入并核对。真实硬件接入需验证断连恢复、重复命令幂等和安全联锁，未经授权不接入危险执行器。'),sub('用户试用与隐私'),para('试用工具包含知情同意、家庭控制任务、异常情景和反馈表。用户回答需保留日期、角色、任务及原话，公开材料匿名化，不采集住址或敏感家庭习惯。尚未收到真实回答的表格保持待采集状态，不生成用户证言。'),para('生产环境须设置日志脱敏、访问权限与留存周期。网关本地部署不自动意味着数据不出户；每项任务是否发送模型端、发送字段和保存位置均需明确。')),
page('五 产业价值与推广',sub('可量化价值与适用客户'),para('优先面向已有本地模型运行条件的家居集成商和开发者，提供统一策略、设备适配和可复核故障记录。价值验收重点是合同完成率、尾时延、远端调用、故障定位时间和部署维护工时。当前实验支持软件机制的可运行性，尚未产生住户体验改善或实际商业收入证据。'),table(['价值指标','采集方法','当前证据'],[['服务连续性','同输入比较成功、拒绝与能力降级','合成和HTTP桩已有'],['运维效率','记录诊断前后人工工时与问题类型','暂无客户实测'],['部署资源','进程RSS、显存、功耗、冷启动和并发','逐机测量，不用MCU对象替代'],['成本收益','计算实际推理费、电费与维护费','报价参数和情景假设单列']],[31,84,35]),sub('成本核算与复制条件'),para('总成本按设备投入摊销、模型主机或共享算力、远端调用、网络、电力及维护分别核算。历史宿主size 28.1 KB仅是端侧目标对象text+data+bss总和，不是完整网关或模型内存，不能据此承诺十元级模型网关。没有真实报价、采购和电力记录时，不发布成本下降比例。'),para('复制包包含C策略、HTTP合同、设备适配、固定输入、原始事件及版本清单。每个目标平台重新校准模型容量、时延预算、动作权限和协议兼容。先验证单类灯光与只读查询，再扩展到窗帘和环境设备；真实高风险设备需专业测试。')),
page('六 单人实施与资源保障',sub('现有能力和组织方式'),para('当前由一名学生负责业务建模、C策略、Python网关、测试和材料。采用需求与指标、实现与构建、实验与审计、材料与发布四条任务清单管理依赖和版本。代码变更后重新构建相关证据，发布前交叉核对数字、引用和文件哈希，不虚构三人团队或合作单位。'),sub('资源与依赖'),para('现有普通电脑可运行软件网关、合成回放、HTTP桩与设备模拟。历史AutoDL GPU运行仅证明当时模型服务完成推理；本轮按用户要求优先使用本机，不将租用实例写作当前持续可用资源。尚无开发板、传感器、通信测试设备和家庭试点。'),sub('原创性与许可'),para('复用SenseHome/openvela原型及第三方运行时，新增联合路由、健康观测适配、动作保护和证据流水线。上游代码、补丁和本项目新增代码分开说明；最终依赖以compliance/NOTICE、许可证与CURRENT_DEPENDENCIES.md为准。模型名称和第三方产品名用于准确标注来源，不将其宣称为自主知识产权。'),sub('生成式AI使用说明'),para('使用生成式AI辅助代码、测试、文档、图表和视频脚本制作，使用本机语音合成制作演示旁白。自动化核验包括构建、原始结果复算、格式与渲染检查，不能替代参赛者的事实复核。最终参赛者须审阅并对数据真实性、原创性、引用与提交内容承担责任。')),
page('七 附录与复核索引',table(['材料','复核内容'],[['evidence/network_compute/20261111_fused/','r3构建、测试、六类实验、原始行、审计与哈希'],['docs/experiments/autodl_20261103_normalized_v2/','真实模型20条、模型配置、原始响应与重新标注'],['evidence/current/dashboard/','旧版160条本地路径、模拟状态及事件'],['docs/video/v2/software_replay.json','2026-11-03历史软件回放，非r3现场录屏'],['compliance/','上游与第三方许可、NOTICE、补丁'],['dist/final/20261111/','本版方案、答辩、佐证与视频']],[78,72]),sub('参考依据'),para('1  ITU-R Recommendation M.2160-0，Framework and overall objectives of the future development of IMT for 2030 and beyond，2023。仅用于IMT-2030方向定位。'),para('2  官方附件1作品提交要求、附件2场景设计方案大纲与评分标准。方案PDF、答辩PDF、佐证合并PDF，演示MP4为3至5分钟。'),para('3  本项目上述原始数据、构建与审计文件。历史真实模型、r3合成回放、HTTP桩及模拟设备分别计量。'),sub('结论与待验收条件'),para('当前交付形成可运行、可解释、可回放的家庭推理路由软件。联合网络与算力在指定合成矩阵中改善合同达标，但HTTP压力场景收益有限，迟滞没有提高达标率。下一阶段以同一网关联测真实模型，并取得真实需求、目标设备、部署成本和真实网络证据后，才评估生产部署与规模推广。'))
]

SLIDES = [
{'title':NAME,'subtitle':'SenseHome  场景设计方案','lines':['任务分级  两段链路  两端算力','可解释降级与可复核运行证据'],'notes':'匿名评审版。'+ABSTRACT},
{'title':'家庭模型控制的连续性需求','lines':['需求对象  存量住宅的家居集成服务商','WAN异常时保留LAN与本地控制能力','模型负载与动作合规同样影响完成'],'foot':'客户画像为设计定位，尚无真实访谈、家庭试点或客户确认','notes':PLAN[1]['blocks'][1]['text']},
{'title':'设备规则 网关模型 远程模型','diagram':True,'notes':'当前网关为PC，设备为JSON模拟。历史AutoDL模型为独立实验，尚未与r3联测。部署角色和接口详见作品方案第三节。'},
{'title':'复杂指令联合使用链路与算力','lines':['任务约束优先  简单控制与只读查询留在规则路径','候选先验收  健康、观测年龄、队列、内存、并发','仅计算已知成本  unknown保持未知','C策略唯一决策  网关负责适配、校验和记录'],'foot':'阈值是项目配置，未来真实网络接口仍需接入','notes':'来源：offload_policy.c、resource_policy.py及r3构建归档。'},
{'title':'四类证据分别计量','table':[['证据层','规模','可以回答的问题'],['合成回放','58,800条','指定轨迹下的策略合同'],['HTTP规划桩','9,084请求','本机网关墙钟与事件完整性'],['历史真实模型','20条','参考动作一致与接口时延'],['PC状态确认','160请求','本地控制模拟状态变化']],'foot':'接口响应、合同完成、模型动作一致和物理执行采用不同定义','notes':'来源：r3 README、AutoDL normalized_v2 README、current/dashboard。'},
{'title':'合成复杂任务达标率','chart':True,'foot':'每臂n=5,040。固定规划桩与建模时延，不是NLU准确率','notes':'joint 84.40%（4254/5040），network-only 62.98%，compute-only 61.90%，no-hysteresis 84.52%。联合仍有786条未解析。迟滞未提升达标率。来源：'+FUSED+'replay/summary.json'},
{'title':'HTTP压力条件下收益有限','table':[['本机HTTP实验','joint','network-only'],['矩阵复杂任务达标','80.95%','61.90%'],['压力组达标','52.98%','52.54%']],'lines':['679条合同未完成，外层响应均为200','296条下游503为注入故障，区别于实网丢包'],'foot':'矩阵2,940请求与压力6,144请求独立统计','notes':'283项HTTP清单匹配。数据不能与合成时延或真实模型结果混算。来源：'+FUSED+'http_gateway/'},
{'title':'独立真实模型已有20条记录','table':[['指标','结果'],['HTTP成功 / 参考动作一致','17/20 / 17/20'],['请求中位数 / p95','290.679 / 522.643 ms'],['预期拒答','3/3另列']],'lines':['Qwen2.5-1.5B-Instruct  RTX 4080 SUPER','标签冻结后重标原响应，未伪称重新推理'],'foot':'历史AutoDL独立实验，尚未与r3端到端联测，无物理执行','notes':PLAN[7]['blocks'][0]['text']+' 来源：'+MODEL},
{'title':'动作保护与状态回放','lines':['28项安全用例  25项拒绝  3项正向控制','模型计划先校验，拒绝后不执行局部危险动作','软件回放展示开灯、只读查询、拒绝与关灯','回放使用历史原始JSON状态，编辑节奏不代表执行时延'],'foot':'PC模拟设备，白名单测试不构成物理安全认证','notes':'来源：r3 safety/summary.json，docs/video/v2/software_replay.json。'},
{'title':'AI与6G的接口位置','table':[['融合接口','软件已实现','后续实网验收'],['链路预算支持推理','WAN/LAN观测进入决策','真实遥测与尾时延'],['任务分级支持协同','安全和结构化任务本地优先','调用量与传输量'],['资源观测支持路由','健康、队列、内存、并发','真实资源校准']],'foot':'IMT-2030方向映射；无6G空口、切片或确定性承载实测','notes':'ITU-R M.2160-0用于方向定位。项目没有实现无线资源分配。'},
{'title':'部署与价值采用逐层验收','lines':['先验证模型网关，再接授权设备与真实链路','同输入比较完成率、尾时延和远端调用','记录安装、诊断和维护工时，形成成本核算','没有真实回答、采购或电量记录时保留待采集'],'foot':'历史28.1 KB为端侧目标对象，不能代替完整网关或模型内存','notes':PLAN[9]['blocks'][1]['text']},
{'title':'单人实现与版本保障','lines':['当前一名学生  策略、网关、实验和材料','r3全量测试通过，运行期间源码未变化','328个运行文件逐路径SHA256一致','公开来源、原始结果与生成材料逐项对应'],'foot':'无虚构团队成员、合作单位、真实家庭或硬件','notes':'团队组织使用任务清单、构建证据和发布校核。生成式AI与合成语音使用如实披露。'},
{'title':'可运行的家庭推理路由软件','lines':['已完成  融合策略、网关闭环、原始证据与审计','已有  独立真实模型动作规划实验','待验收  联合真实推理、需求、物理设备与实网','交付  代码、固定输入、逐条结果与复核入口'],'foot':'面向实际需求的场景设计方案  2026年11月11日','notes':'结论依据与限制参见方案第七节。下一步依据真实数据推进，不将设计类证据写成落地成效。'}
]

def local_insert():
    p=Path(__file__).with_name('local_result_insert.json')
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None

# Public sources and resource observations were verified by the independent value line.
PLAN[1]['blocks'][-1] = para('Home Assistant官方Assist、本地pipeline、Ollama与AI agent文档表明，本地自然语言、模型接入和本地意图优先均已有实现，不能作为本作品首创。RouteLLM已有成本与质量驱动的模型路由。ITU-R M.2160-0与ETSI MEC支持通信计算协同和边缘信息接口方向。上述官方材料支持技术背景，不代表住户需求、客户愿付价或本项目性能优势。9项来源的URL、摘录、访问日期和哈希见docs/value/public_sources.json。')
PLAN[9]['blocks'][-2] = para('成本工具按硬件摊销、电费、云token、维护和网络费用分项，输入标记实测、报价、假设或未知。月总成本C=P/M+E×电价+云token费+维护+网络，每千总请求成本为1000C/N。任一必要分项缺失时，总成本保持null。现无采购价、电表积分或实际维护工时，不能给出实测节费比例。历史28.1 KB仅为端侧目标对象，不是完整网关或模型内存。')
PLAN[10]['blocks'][3] = para('本轮只使用现有Windows PC，无新租云。30样本基线中，小模型和大模型进程RSS中位分别为1,170,309,120 B、2,724,466,688 B；共享RTX 4050 Laptop整卡显存6,141 MiB、已用3,735 MiB；RAM可用最低906,969,088 B。该短时常驻采样不代表最低配置或长期并发能力，板卡遥测不等同整机电量。原件见docs/experiments/20261111_local_resources/baseline.json。')
PLAN[11]['blocks'].insert(-2, para('4  Home Assistant官方Assist、本地pipeline、Ollama、AI agent与OpenAI文档；RouteLLM官方仓库；ETSI MEC。2026-11-11读取，URL、摘录与支持范围见docs/value/public_sources.json。对标与成本复算见docs/value/，未开展同条件竞品性能测试。'))
SLIDES[1]['notes'] += ' 官方需求技术依据为Home Assistant Assist/本地pipeline/Ollama/AI agent文档，尚无真实访谈。来源docs/value/public_sources.json S1-S5。'
SLIDES[3]['notes'] += ' 本地意图优先与LLM回退在Home Assistant已有先例，不作为本项目首创。'
SLIDES[10]['lines'] = ['本机双模型RSS中位约1.17 GB与2.72 GB','共享显存已用3,735 MiB，短时采样不代表最低配置','成本模型含硬件、电量、调用、维护与网络','必要成本未知则总额为null，不发布实测节费比例']
SLIDES[10]['notes'] = '来源：docs/value/host_resources_summary.json、cost_model.md、cost_results/current_unknown.json。30样本短时常驻采样，RTX 4050 Laptop整卡已用显存不能全归因系统。没有电表积分、采购和真实维护工时。'

# Lock the final post-fix run. Intermediate/pre-fix evidence remains archived separately.
ABSTRACT = ('家庭智能体调用远程模型时，网络、模型负载与输出合规共同决定任务能否完成。SenseHome面向存量住宅集成服务，设计设备规则、网关模型和远程模型三档能力，以纯C策略联合使用WAN/LAN及两端服务观测选择后端，执行动作白名单、故障回退和事件记录。融合版r3完成全量构建与审计，复杂任务合成时限内合同完成率为联合84.40%、仅网络62.98%；无迟滞84.52%，不支持迟滞提升完成率。本轮20条冻结命令、四臂、三次重复产生240条本机记录。每臂21次复杂模型请求中，联合、仅网络和固定小模型均15次非空模拟执行完成，第二本机4B候选也是15次；正常健康条件下未证明联合优势。独立历史AutoDL真实模型动作一致17/20。各来源分别统计，当前设备为PC模拟状态，双模型共享本机GPU，无真实家庭、物理执行或6G空口证据。方案以可解释降级与可复核记录为基础，后续逐层验收需求、独立部署、网络和成本。')
PLAN[0]['blocks'][5] = para(ABSTRACT)
LOCAL_PAGE=page('四 本机真实模型四臂联测',para('正式运行：2026年11月11日，20条冻结人工标签×3次重复×4臂，共240条；运行器失败0、跳过0。相同命令、上下文、初始状态和冻结健康快照，策略臂顺序轮换；各臂均让简单与安全类任务使用本地规则。'),table(['复杂模型臂','结构匹配','非空完成','保护拒绝','网关p50 / p95 ms'],[['fixed-local','15/21','15/21','6','581.496 / 1238.693'],['fixed-cloud代理','18/21','15/21','3','614.036 / 957.479'],['network-only','15/21','15/21','6','619.873 / 1033.887'],['joint','15/21','15/21','6','600.715 / 1043.262']],[35,22,21,20,52]),para('每臂另有39条规则请求，动作结构39/39匹配，其中21条为非空任务模拟完成，18条为空结构匹配，不解释为澄清正确。复杂模型请求各臂上游HTTP均21/21成功；保护拒绝单列，不把HTTP 200计为全部完成。4B臂18个结构匹配包含3个空结构，非空完成仍是15次。'),sub('本机条件下的结论'),para('Qwen2.5-1.5B-Instruct-Q4_K_M与Qwen3-4B-Q4_K_M共享同机GPU，fixed-cloud只是第二端点代理，不是真实两地端云。服务端标识与revision来源独立记录，未知版本保持unknown。当前正常健康条件没有证明联合策略优势，也不能复现r3的合成达标收益。'),para('时延为网关墙钟，共享探测在网关外单列；模型清单HTTP往返不是无线RTT。重复行相关，仅作描述统计。运行调用既有C二进制并记录哈希，不能只据哈希宣称源码到二进制可复现构建。来源：'+LOCAL+'report.json与summary.json。'))
PLAN.insert(8, LOCAL_PAGE)
PLAN[7]['blocks'][4] = sub('与本轮证据的关系')
PLAN[7]['blocks'][5] = para('本轮另有240条本机四臂联测，使用新日期、共享本机模型与独立来源记录，详见下一页。历史AutoDL的模型、运行时和环境不同，二者不合并为更大的准确率或时延样本。')
PLAN[2]['blocks'][1]['rows'].insert(3,['本机四臂真实模型','240条；各臂复杂任务非空完成15/21','本机共享GPU；规则单列'])
PLAN[9]['blocks'][0]['rows'][2][2] = '已完成240条正式联测'
PLAN[-1]['blocks'][-1] = para('当前交付形成可运行、可解释、可回放的家庭推理路由软件。联合网络与算力在指定合成矩阵中改善合同达标；HTTP压力场景收益有限，迟滞没有提高达标率。本机真实模型联测已形成240条记录，正常健康条件下未证明联合优势。后续取得真实需求、目标设备、独立网络和成本证据后，再评估生产部署与推广。')
SLIDES[4]['table'].insert(4,['本机真实联测','240条','四臂、3重复、模拟状态完成'])
SLIDES[4]['notes'] += ' 新正式本机运行：'+LOCAL+'summary.json。'
LOCAL_SLIDE={'title':'本机四臂已完成240条联测','table':[['复杂模型臂','结构匹配','非空模拟完成','保护拒绝'],['固定小模型','15/21','15/21','6'],['第二本机4B代理','18/21','15/21','3'],['仅网络 / 联合','各15/21','各15/21','各6']],'lines':['每臂另39条规则请求，18个空结构不称澄清正确','正常健康条件下没有证明联合策略优势'],'foot':'同机共享GPU，无真实WAN；20命令×3重复×4臂，失败0、跳过0','notes':'来源：'+LOCAL+'summary.json、report.json。上游复杂模型HTTP均21/21成功，接口/保护/完成分开。固定4B的18个结构匹配含3个空结构，非空完成仍15。规则39条中21为非空模拟完成，18为空结构匹配。网关p50/p95小模型581.496/1238.693，4B614.036/957.479，仅网络619.873/1033.887，联合600.715/1043.262ms。共享探测在网关外另列，重复样本相关，无优势统计推断。'}
SLIDES.insert(8,LOCAL_SLIDE)
SLIDES[-1]['lines'] = ['已完成  融合策略、网关闭环、原始证据与审计','新增  240条本机真实模型联测，正常条件无联合优势','待验收  真实需求、独立网络、物理设备与成本','交付  代码、固定输入、逐条结果与复核入口']
PLAN[0]['blocks'][-1] = para('当前成熟度  软件原型与分层验证，真实家庭与物理设备尚未试点。')
LOCAL_PAGE['blocks'][2] = para('每臂另39条规则请求，结构39/39匹配，其中21条非空模拟完成、18条空结构匹配，不解释为澄清正确。复杂模型上游HTTP均21/21成功，保护拒绝单列。4B臂的18次匹配含3个空结构，非空完成仍为15次。')
LOCAL_PAGE['blocks'][4] = para('Qwen2.5-1.5B与Qwen3-4B均为Q4_K_M量化，共享本机GPU；fixed-cloud只是第二端点代理，不是真实两地端云。未知revision保持unknown。正常健康条件没有证明联合优势，不能复现r3合成达标收益。')
LOCAL_PAGE['blocks'][5] = para('时延为网关墙钟，共享探测在网关外单列；HTTP清单往返不是无线RTT。重复行相关，仅作描述统计。正式运行来源见附录“本机联测”，源码与既有C二进制哈希分别保存。')
TEAM_PAGE=next(p for p in PLAN if p['title'].startswith('六 '))
appendix=PLAN[-1]
appendix['blocks'][0]['rows'].insert(2,[LOCAL,'本机正式联测240条、配置、逐条响应、事件与摘要'])
references=appendix['blocks'][1:-2]
appendix['blocks']=[appendix['blocks'][0],*appendix['blocks'][-2:]]
PLAN.append(page('参考依据与公开对标',*references,para('公开对标资料仅用于证明技术路径和已有能力。尚未开展竞品同条件性能测试，不发布“优于Home Assistant”或套用RouteLLM节费比例。'),para('本项目需求、部署与成本工具：docs/value/demand_and_benchmark.md、ai6g_mapping.md、five_minute_trial.md、deployment_and_replication.md、cost_model.md。真实回答、实际成本和试点证明尚未取得，继续以原始采集为验收条件。')))
PLAN[3]['blocks'][1]['rows'][3][2] = 'wrapper /infer；本机OpenAI兼容直连接口'
SLIDES[4]['title']='证据分层计量'

def apply_sealed_run():
    """Read only an explicitly selected final run; never select by recency."""
    global LOCAL
    cfg=local_insert()
    if not cfg or not cfg.get('run_path'):return
    previous=LOCAL
    LOCAL=cfg['run_path'].rstrip('/')+'/'
    summary=json.loads((ROOT/LOCAL/'summary.json').read_text(encoding='utf-8'))
    report=json.loads((ROOT/LOCAL/'report.json').read_text(encoding='utf-8'))
    assert report['status']=='finished' and summary['n_rows']==240 and summary['n_skipped']==0 and summary['n_runner_failed']==0
    arms=summary['by_arm_and_evidence']; rows=[]
    for arm,label in [('fixed-local','fixed-local'),('fixed-cloud','fixed-cloud代理'),('network-only','network-only'),('joint','joint')]:
        a=arms[arm]['real-model'];n=a['n_rows']
        rows.append([label,f"{a['n_action_correct']}/{n}",f"{a['n_completed']}/{n}",str(a['n_protection_refusal']),f"{a['gateway_median_ms']:.3f} / {a['gateway_p95_ms']:.3f}"])
    LOCAL_PAGE['blocks'][1]['rows']=rows
    sr=arms['joint']['local-rules']
    LOCAL_PAGE['blocks'][2]=para(f"每臂另{sr['n_rows']}条规则请求，结构{sr['n_action_correct']}/{sr['n_rows']}匹配，其中{sr['n_completed']}条非空模拟完成、{sr['n_empty_expected_structure_match']}条空结构匹配，不解释为澄清正确。复杂模型上游HTTP与保护拒绝分列。固定4B的结构匹配包含空结构，不能直接解释为非空完成。")
    LOCAL_SLIDE['table']=[['复杂模型臂','结构匹配','非空模拟完成','保护拒绝']]+[[label,rr[1],rr[2],rr[3]] for label,rr in zip(['固定小模型','第二本机4B代理','仅网络','联合'],rows)]
    # Adjust notes and visible references to the one locked run.
    for pp in PLAN:
        for bb in pp['blocks']:
            if 'text' in bb:bb['text']=bb['text'].replace(previous,LOCAL)
            if bb['kind']=='table':bb['rows']=[[str(x).replace(previous,LOCAL) for x in row] for row in bb['rows']]
    for ss in SLIDES:ss['notes']=ss['notes'].replace(previous,LOCAL)
    LOCAL_SLIDE['notes']='正式封存运行 '+LOCAL+'summary.json、report.json。复杂臂逐项：'+json.dumps(rows,ensure_ascii=False)+'。规则与空结构另列；同机共享GPU，非真实云边；重复相关，仅描述统计。'
    if cfg.get('development_run'):
        dr=cfg['development_run'].rstrip('/')+'/'
        LOCAL_PAGE['blocks'].append(para('补充开发回归与故障条件原始记录见 '+dr+'。该程序派生命令已用于修复，不作为未见泛化测试；故障观测为显式软件注入，不作为实网数据。'))
apply_sealed_run()
HOLDOUT_PAGE=page('补充 预注册程序组合留出检查',
    para('在生产实现、提示词、校准与配置冻结后，按seed=2026111126生成12条无原文交集的程序标签命令，先注册后推理，只运行一次。四臂各3次重复共144行，真实模型POST为132次，0跳过、0运行器异常。缺房间H09共12行直接走规则，不调用模型。'),
    table(['策略','非空模拟完成','空目标无动作且状态不变','保护拒绝'],[
        ['固定小模型','21/24','12/12','9'],['第二本机4B','24/24','12/12','6'],
        ['仅网络','21/24','12/12','9'],['联合','21/24','12/12','9']],[35,35,57,23]),
    para('H05要求开卧室灯、关客厅窗帘。小模型把窗帘关闭写为off，而动作合同要求close；3次重复、3个臂共9条非空任务被白名单422拒绝，失败全部保留，结果产生后未再调优。本矩阵不支持联合策略质量更高。'),
    para('标签来自程序组合，未经人工逐条核对；这不是人工语言保留集或真实用户试用。重复请求相关，空目标保护不代表澄清话术正确。两个模型仍共享本机GPU，设备为PC模拟状态。'),
    para('注册先于运行，11项源码/二进制、数据、校准与配置哈希相符。来源：docs/experiments/20261111_fused_real/holdout_12_preregistered/registration.json及holdout_12_programmatic_repeat3/；四套结果独立统计，导航见sealed_evidence_index.json。'))
PLAN.append(HOLDOUT_PAGE)
PLAN.append(page('附图 架构与业务流程',para('图中设备为PC模拟状态，两个模型候选当前共享本机资源；接口可适配wrapper或OpenAI兼容服务。'),{'kind':'image','path':'SenseHome-6G/figures/submission_20261111_architecture.png','caption':'图1 部署角色与后端适配边界'},{'kind':'image','path':'SenseHome-6G/figures/submission_20261111_workflow.png','caption':'图2 任务路由 计划校验 状态执行与事件记录'},para('动作被拒绝、后端异常与规则能力不足分别记录；具有接口响应不等于任务完成。')))

def render_markdown(pages):
    out=[]
    for p in pages:
        out.append('# '+p['title'])
        for b in p['blocks']:
            if b['kind']=='h2':out.append('## '+b['text'])
            elif b['kind']=='p':out.append(b['text'])
            elif b['kind']=='image':out.append('!['+b['caption']+']('+b['path'].removeprefix('SenseHome-6G/')+')')
            else:
                out.append('| '+' | '.join(b['headers'])+' |')
                out.append('| '+' | '.join(['---']*len(b['headers']))+' |')
                out.extend('| '+' | '.join(r)+' |' for r in b['rows'])
    return '\n\n'.join(out)+'\n'
