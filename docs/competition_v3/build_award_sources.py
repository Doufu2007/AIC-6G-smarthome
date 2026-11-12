"""Create bounded, source-located excerpts for award comparison research."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locate(path, quote):
    text = path.read_text(encoding='utf-8')
    at = text.find(quote)
    if at < 0:
        raise ValueError(f'Quote not found in {path.name}: {quote}')
    return text[:at].count('\n') + 1


def main():
    docs = [
        {'id': 'S0', 'title': '首届人工智能+6G青年创新创业专项赛',
         'issuer': 'AIC大赛组委会', 'date_displayed': '2026-09-28',
         'url': 'https://www.aicomp.cn/tracks/tracks-4/5322.html',
         'raw': '.firecrawl/v3-same-contest.md',
         'quotes': ['首届人工智能+6G青年创新创业专项赛',
                    '（四）决赛（2026年11月底-12月中上旬）',
                    '参赛作品可为已落地实施的应用场景解决方案，也可为面向实际需求的应用场景设计方案。']},
        {'id': 'S1', 'title': 'SAI teams claim top honors at national embedded chip competition',
         'issuer': '深圳技术大学人工智能学院', 'date_displayed': '2026-09-21',
         'url': 'https://ai.sztu.edu.cn/info/1421/6896.htm', 'raw': '.firecrawl/v3-sztu-embedded.md',
         'quotes': ['The teams brought home two national first prizes, three second prizes and two third prizes.',
                    'Smart Conference Room System Based on OpenHarmony and RISC-V',
                    'supporting StarFlash and Bluetooth communication, template editing, device management and batch configuration distribution.',
                    'WS63‑based Wireless Data Transmission System for Power Equipment',
                    'the system connects Modbus RTU devices such as single-/three-phase electric meters, temperature-humidity transmitters and relays.']},
        {'id': 'S2', 'title': '特等奖！一等奖！港科大（广州）学子闪耀这项全国大赛',
         'issuer': '香港科技大学（广州）', 'date_displayed': '2025-08-15',
         'url': 'https://www.hkust-gz.edu.cn/zh/2025/08/15/%E7%89%B9%E7%AD%89%E5%A5%96%EF%BC%81%E4%B8%80%E7%AD%89%E5%A5%96%EF%BC%81%E6%B8%AF%E7%A7%91%E5%A4%A7%EF%BC%88%E5%B9%BF%E5%B7%9E%EF%BC%89%E5%AD%A6%E5%AD%90%E9%97%AA%E8%80%80%E8%BF%99%E9%A1%B9%E5%85%A8/?variant=zh-cn',
         'raw': '.firecrawl/v3-hkust-edge.md',
         'quotes': ['**全国一等奖（C赛道）：**',
                    '**智护夜巡机器人——基于大语言模型的创新性护理机器人**',
                    '开始搭建一款具备规划、感知和问询三大核心功能的“护士天使机器人”。',
                    '参加香港老年人展会，让他们亲身体会老龄用户的实际需求']},
        {'id': 'S3', 'title': '我院学子在全国大学生嵌入式芯片与系统设计竞赛中荣获一等奖',
         'issuer': '厦门大学电子科学与技术学院', 'date_displayed': '2021-11-17',
         'url': 'https://ese.xmu.edu.cn/info/1052/6399.htm', 'raw': '.firecrawl/v3-xmu-embedded.md',
         'quotes': ['厦门大学“凰月流芯”学生创新团队斩获全国总决赛一等奖。',
                    '参赛作品“Taurus无人面包房”',
                    '作品由Taurus自助收银终端、面包识别台、和Linux服务器三个子系统组成。',
                    '作品较好的完整性和较高的展示度获得了评委的关注与好评']},
        {'id': 'S4', 'title': '物联学子斩获第七届全球校园人工智能算法精英大赛全国一等奖',
         'issuer': '江南大学自动化与智能科学学院（物联网学院）', 'date_displayed': '2025-12-31',
         'url': 'https://auto.jiangnan.edu.cn/info/1033/4611.htm', 'raw': '.firecrawl/v3-jiangnan-aic.md',
         'quotes': ['斩获全国一等奖1项、二等奖1项、三等奖1项的优异成绩。',
                    '依托搭载边缘算力板卡、激光雷达及深度相机的实体智能机器人']},
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    excerpts = OUT / 'award_excerpts'
    excerpts.mkdir(exist_ok=True)
    for doc in docs:
        raw = ROOT / doc['raw']
        doc['raw_sha256'] = digest(raw)
        doc['retrieved_local_date'] = '2026-11-11'
        doc['excerpt_locations'] = [{'quote': q, 'raw_start_line': locate(raw, q)} for q in doc.pop('quotes')]
        excerpt = excerpts / (doc['id'] + '.md')
        lines = [f"# {doc['title']}", '', f"发布机构：{doc['issuer']}",
                 f"页面显示日期：{doc['date_displayed']}", f"来源：{doc['url']}",
                 f"读取日期：{doc['retrieved_local_date']}", f"原始快照SHA256：{doc['raw_sha256']}", '',
                 '仅保存与比较相关的短摘录；未将图片、虚拟附件链接或页面导航纳入交付。', '']
        for item in doc['excerpt_locations']:
            lines += [f"原始快照第{item['raw_start_line']}行：", '', '> ' + item['quote'], '']
        excerpt.write_text('\n'.join(lines), encoding='utf-8')
        doc['excerpt_path'] = excerpt.relative_to(ROOT).as_posix()
        doc['excerpt_sha256'] = digest(excerpt)
    cases = [
        {'id': 'A1', 'source_id': 'S1',
         'original_project_name': 'Smart Conference Room System Based on OpenHarmony and RISC-V',
         'translated_name': '基于OpenHarmony与RISC-V的智慧会议室系统（本项目译名）',
         'competition': '第九届全国大学生嵌入式芯片与系统设计竞赛', 'award': '全国一等奖（高校官方报道）',
         'implementation_reported': ['软硬件一体会议室系统', '电子墨水名牌与跨平台中控', '星闪/蓝牙通信、设备管理与批量配置'],
         'not_reported_or_not_verified': ['批量部署实测原始记录', '住户或客户验收', '完整成本账单', '代码/同条件测评', '独立复现实验'],
         'valid_comparison': '用户能完成的最小业务闭环、真实协议接入与可演示管理操作',
         'invalid_comparison': '不能因其获奖而认定本赛也必须有这些硬件；不能借新闻中的分钟级表述证明SenseHome时延优劣'},
        {'id': 'A2', 'source_id': 'S1',
         'original_project_name': 'WS63‑based Wireless Data Transmission System for Power Equipment',
         'translated_name': '基于WS63的电力设备无线数据传输系统（本项目译名）',
         'competition': '第九届全国大学生嵌入式芯片与系统设计竞赛', 'award': '全国一等奖（高校官方报道）',
         'implementation_reported': ['星闪DTU接入Modbus RTU电表、温湿度变送器、继电器', '根/中继/叶多级无线组网', '边缘聚合/压缩/分析、云端AI异常分析与联动'],
         'not_reported_or_not_verified': ['真实现场部署地点与规模', '无线丢包/尾时延原始记录', '完整成本对照', 'AI模型准确率', '独立复现'],
         'valid_comparison': '通信技术在方案中承担明确链路角色、协议与设备清单完整、边缘与AI各有职责',
         'invalid_comparison': '星闪成果不能充作6G验证；宣传的稳定/可靠/节费未给原始数据，不能当数值门槛'},
        {'id': 'A3', 'source_id': 'S2',
         'original_project_name': '智护夜巡机器人——基于大语言模型的创新性护理机器人',
         'competition': '第四届高校电气电子工程创新大赛C赛道', 'award': '全国一等奖（高校官方报道）',
         'implementation_reported': ['规划、感知、问询三项核心功能的实体机器人', '语音、感知定位、系统集成', '团队参加老年人展会了解实际需求、决赛现场展示与答辩'],
         'not_reported_or_not_verified': ['养老院长期部署', '医护安全/临床认证', '量化任务成功率或照护效果', '客户付费', '长期成本'],
         'valid_comparison': '围绕一个可解释业务缺口、明确角色需求、对“为什么不用现有方案”的答辩证据',
         'invalid_comparison': '展会接触不等于正式用户试验，新闻价值叙事不等于实测；不能把机器人能力列成本赛强制项'},
        {'id': 'A4', 'source_id': 'S3', 'original_project_name': 'Taurus无人面包房',
         'competition': '2021年第四届全国大学生嵌入式芯片与系统设计竞赛', 'award': '全国总决赛一等奖（高校官方报道）',
         'implementation_reported': ['“放、支付”两步结算', '收银终端、识别台、Linux服务器三个子系统', '海思NNIE检测、Qt交互/语音、支付方式与销售数据库'],
         'not_reported_or_not_verified': ['商用支付结算授权与真实交易', '门店正式部署', '识别率/吞吐/成本完整数据', '完整源代码', '独立复现'],
         'valid_comparison': '把技术收敛为短而清楚的用户动作，并展示从输入、推理到结果存储的端到端流程',
         'invalid_comparison': '不把该项目的支付方式或硬件投入当本赛门槛；文章无可复算量化结果，不能直接排性能名次'},
    ]
    result = {'schema': 'sensehome.award-comparison-sources.v3',
              'created_at_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'official public reports; achievement descriptions, not independent audits or this competition judging criteria',
              'same_track_historical_first_prize_available': False,
              'same_track_reason': 'Official notice explicitly calls this the first edition; as of local 2026-11-11, finals are scheduled for late Nov to mid-Dec 2026. No prior edition can be used as the same-track historical benchmark.',
              'n_comparison_cases': len(cases), 'n_distinct_case_documents': 3,
              'caution': 'A1/A2 share one report, not two independent sources. S4 is context from the prior umbrella competition; no exact project title is disclosed, so it is excluded from the four named case comparisons.',
              'sources': docs, 'cases': cases}
    (OUT / 'award_sources.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'cases': len(cases), 'sources': len(docs), 'all_short_quotes_found_verbatim': True,
                      'output': 'docs/competition_v3/award_sources.json'}, ensure_ascii=True))


if __name__ == '__main__':
    main()
