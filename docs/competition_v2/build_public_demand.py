"""Verify small public-user excerpts against the fetched source snapshots."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
RECORDS = [
    {
        'id': 'P1', 'url': 'https://github.com/home-assistant/core/issues/123316',
        'title': 'Home Assistant core issue 123316',
        'displayed_date': 'August 2024; historical version core-2024.8.0',
        'snapshot': '.firecrawl/v2-demand-ha-123316.md',
        'quote': "Now the model just hallucinates entities that don't exist, and refuses to identify / describe / control them.",
        'observation': '历史使用者报告实体幻觉与控制失败。',
        'requirement': '将模型计划与实体/动作许可分开，拒绝不在能力合同内的执行。',
        'limit': '历史版本公开问题报告；不是当前Home Assistant缺陷证明，也未在本项目环境复现。',
    },
    {
        'id': 'P2', 'url': 'https://community.home-assistant.io/t/my-journey-to-a-reliable-and-enjoyable-locally-hosted-voice-assistant/944860/35',
        'title': 'Public local voice assistant experience, post 35',
        'displayed_date': 'Jan 8; rendered excerpt does not display the year',
        'snapshot': '.firecrawl/v2-demand-journey.md',
        'quote': 'I faced some issues with covers, opening and closing them with llm was not possible (it said it can’t do it) but with local first enabled ha agent did it.',
        'observation': '公开使用者报告同一窗帘操作在LLM与本地意图路径上能力不同。',
        'requirement': '区分规则与模型能力，保留本地明确控制；记录能力损失，不把路径可达当作任务完成。',
        'limit': '公开自述，未验证具体配置、因果或发生频率；不是本项目使用者，也不表示其愿意采用本项目。',
    },
    {
        'id': 'P3', 'url': 'https://community.home-assistant.io/t/my-journey-to-a-reliable-and-enjoyable-locally-hosted-voice-assistant/944860/35',
        'title': 'Same public post 35, latency observation',
        'displayed_date': 'Jan 8; rendered excerpt does not display the year',
        'snapshot': '.firecrawl/v2-demand-journey.md',
        'quote': 'tts and stt take less than 0.1s but the llm response takes from 1 to 11 seconds based on the size and difficulty of the generation.',
        'observation': '同一公开使用者自述生成长度和难度影响响应时间。',
        'requirement': '分别测量端到端、模型与代理等待时间；在规定条件中比较路由，保留超时与失败。',
        'limit': '时间数字是他人自述，不能当作本项目实测、统一基线、普遍需求阈值或性能优势。P2/P3为同一帖子，不算两名参与者。',
    },
]


def main():
    output = []
    for entry in RECORDS:
        source = ROOT / entry['snapshot']
        raw = source.read_bytes()
        text = raw.decode('utf-8')
        assert entry['quote'] in text, entry['id']
        before, after = text.split(entry['quote'], 1)
        start_line = before.count('\n') + 1
        context = text.splitlines()[max(0, start_line - 3):start_line + 2]
        record = {**entry, 'raw_snapshot_sha256': hashlib.sha256(raw).hexdigest(),
                  'quote_start_line': start_line, 'context': '\n'.join(context),
                  'retrieved_local_date': '2026-11-11', 'timezone': 'Asia/Shanghai'}
        # Store short excerpts; raw scraped navigation/avatar URLs are unnecessary.
        excerpt = OUT / 'public_excerpts' / (entry['id'] + '.md')
        excerpt.parent.mkdir(exist_ok=True)
        excerpt.write_text('# ' + entry['title'] + '\n\n' + entry['url'] + '\n\n'
                           + entry['displayed_date'] + '\n\n> ' + entry['quote']
                           + '\n\n' + entry['limit'] + '\n', encoding='utf-8')
        record['excerpt_path'] = excerpt.relative_to(ROOT).as_posix()
        record['excerpt_sha256'] = hashlib.sha256(excerpt.read_bytes()).hexdigest()
        output.append(record)
    result = {'schema': 'sensehome.public-user-evidence.v1', 'scope': 'public secondary user reports; no contact or recruitment',
              'n_distinct_public_documents': 2, 'n_technical_observations': 3,
              'project_participant_feedback_count': 0, 'records': output}
    (OUT / 'public_user_reports.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Verified 3 excerpts in 2 public documents; project feedback count remains 0.')


if __name__ == '__main__':
    main()
