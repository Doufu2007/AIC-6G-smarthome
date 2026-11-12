"""Build a clearly labelled current evidence replay, without any UI automation.

All cards are editorial drawings from UTF-8 evidence, never interface screenshots.
Timing printed on cards comes from records; scene duration is editing cadence only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import wave

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
OUT = ROOT / 'dist/final/20261111_v4'
VIDEO_NAME = 'SenseHome_当前软件证据回放_20261111_v4.mp4'
HEADER = '真实运行数据回放 · 非连续现场录屏 · 软件实体非实物'
DURATIONS = [24, 44, 28, 32, 34, 34, 32, 22]
W, H = 1920, 1080
BG, PANEL, INK, MUTED, CYAN, GOLD = '#081222', '#12243a', '#f2f5f8', '#b2bfd0', '#77dfd6', '#f2c578'
FONT = Path('C:/Windows/Fonts/msyh.ttc')
BOLD = Path('C:/Windows/Fonts/msyhbd.ttc')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rel(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def run(args, *, log=None):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', cwd=ROOT)
    if log:
        with Path(log).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'argv': [str(a) for a in args], 'returncode': result.returncode}, ensure_ascii=False) + '\n')
            stream.write(result.stdout + result.stderr + '\n')
    if result.returncode:
        raise RuntimeError(result.stderr[-6000:])
    return result.stdout


def binaries():
    ff = shutil.which('ffmpeg') or 'C:/Program Files/EVCapture/ffmpeg.exe'
    fp = shutil.which('ffprobe') or 'C:/Program Files/EVCapture/ffprobe.exe'
    if not Path(ff).is_file():
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    if not Path(fp).is_file():
        raise FileNotFoundError('ffprobe is required for final duration/size validation')
    return ff, fp


def font(size, bold=False):
    return ImageFont.truetype(str(BOLD if bold else FONT), size)


def wrapped(draw, text, selected_font, width):
    result = []
    for paragraph in str(text).split('\n'):
        line = ''
        for char in paragraph:
            if line and draw.textlength(line + char, font=selected_font) > width:
                result.append(line)
                line = char
            else:
                line += char
        result.append(line)
    return result


def card(draw, box, title, lines):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, 24, fill=PANEL, outline='#29415c', width=2)
    draw.text((x1 + 34, y1 + 26), title, font=font(33, True), fill=CYAN)
    y = y1 + 92
    for item in lines:
        text, style = item if isinstance(item, tuple) else (item, 'body')
        size, color = {'body': (33, INK), 'muted': (27, MUTED), 'metric': (45, GOLD), 'code': (28, INK)}[style]
        selected = font(size, style == 'metric')
        for line in wrapped(draw, text, selected, x2 - x1 - 68):
            if y + size > y2 - 20:
                raise ValueError('CARD_CONTENT_OVERFLOW: ' + title + ' / ' + str(text))
            draw.text((x1 + 34, y), line, font=selected, fill=color)
            y += size + 13
        y += 9


def render(path, scene_no, title, subtitle, cards, source, *, variant=''):
    image = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, W, 78), fill='#163047')
    draw.text((70, 21), HEADER, font=font(30, True), fill=GOLD)
    draw.text((1740, 21), 'v4', font=font(30, True), fill=CYAN)
    draw.text((80, 112), f'{scene_no:02d} / 08   SENSEHOME', font=font(26, True), fill=CYAN)
    draw.text((80, 165), title, font=font(55, True), fill=INK)
    draw.text((82, 245), subtitle, font=font(29), fill=MUTED)
    count, gap = len(cards), 28
    width = (1760 - gap * (count - 1)) // count
    for index, (heading, lines) in enumerate(cards):
        x = 80 + index * (width + gap)
        card(draw, (x, 310, x + width, 924), heading, lines)
    draw.text((82, 951), '来源：' + source, font=font(23), fill=MUTED)
    draw.text((82, 997), '镜头停留是编辑节奏；画面中的毫秒数来自原始运行记录。', font=font(25), fill=GOLD)
    for i in range(8):
        x = 1430 + i * 48
        draw.rounded_rectangle((x, 1006, x + 32, 1014), 4, fill=CYAN if i < scene_no else '#32465d')
    image.save(path)
    return {'path': rel(path), 'scene': scene_no, 'title': title, 'variant': variant}


def number(value, decimals=3):
    return '未知' if value is None else f'{float(value):.{decimals}f}'


def actual_actions(record):
    return record['response']['body'].get('actions', [])


def action_line(action):
    params = action.get('params', {})
    details = ', '.join(f'{k}={v}' for k, v in params.items() if k != 'location')
    return f"{action.get('name')} | {params.get('location', '未指明')} {details}"


def light_state(record, when, room):
    value = record[when]['body']['lights'][room]
    return str(value['brightness']) + '%'


def load_bundle(records_path, manifest_path):
    # Never silently substitute a historical or interim capture for final data.
    if not records_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError('Waiting for final_runtime_records.json and final_runtime_manifest.json')
    current = read(records_path)
    if current.get('schema') != 'sensehome.video-source.v4' or len(current.get('records', [])) != 4:
        raise ValueError('EXPECTED_FOUR_CURRENT_HTTP_RECORDS')
    for row in current['records']:
        if not isinstance(row.get('command'), str) or '�' in row['command']:
            raise ValueError('INVALID_UTF8_COMMAND')
        if row.get('record_kind') != 'automation_test':
            raise ValueError('CURRENT_CAPTURE_MUST_REMAIN_AUTOMATION_TEST')
    sources = [records_path, manifest_path, ROOT / 'docs/experiments_v4/registration.json',
               ROOT / 'docs/experiments_v4/results.json', ROOT / 'docs/experiments_v4/rows.jsonl',
               ROOT / 'docs/operations_v4/results.json', ROOT / 'docs/integration_v4/LATEST.json']
    integration_dir = ROOT / read(sources[-1])['run_dir']
    sources += [integration_dir / 'summary.json', integration_dir / 'offline_audit.json',
                integration_dir / 'normal/http_results.json', integration_dir / 'ack_loss/http_results.json']
    rows = [json.loads(line) for line in sources[4].read_text(encoding='utf-8').splitlines() if line]
    results = read(sources[3])
    assert len(rows) == results['n_rows'] == 96
    assert read(integration_dir / 'offline_audit.json')['pass'] is True
    return {'current': current, 'runtime': read(manifest_path), 'registration': read(sources[2]),
            'results': results, 'rows': rows, 'operations': read(sources[5]), 'integration': read(integration_dir / 'summary.json'),
            'audit': read(integration_dir / 'offline_audit.json'), 'sources': sources, 'integration_dir': integration_dir}


def prepare_scenes(bundle, work):
    r = bundle['current']['records']
    results, ops = bundle['results'], bundle['operations']
    frames, scenes, narration = [], [], []

    def image(scene, tag, title, subtitle, cards, source):
        path = work / f'scene_{scene:02d}_{tag}.png'
        frames.append(render(path, scene, title, subtitle, cards, source, variant=tag))
        return path

    def scene(paths, speech):
        scenes.append(paths)
        narration.append(speech)

    p = image(1, 'overview', '让家庭 AI 的每一步都有据可查', '本轮软件运行记录 + 实验证据说明  |  2026-11-11', [
        ('统一执行链路', ['自然语言 → C 策略初选', '规则 / 本机模型 / 第二模型', '完整动作校验 → MQTT', '软件实体状态 + 持久回执', ('原始事件、SQLite 和源码哈希可追溯', 'muted')]),
        ('本片证据边界', ['真实本机 HTTP 与真实模型调用', '真实 loopback MQTT 协议', '设备为独立软件实体', '当前没有物理执行或 6G 空口实测', ('画面为数据摘录卡，不是界面截图', 'muted')]),
    ], 'final_runtime_records.json；final_runtime_manifest.json')
    scene([(p, 24)], '本作品是 SenseHome，面向家庭场景的可审计推理路由软件。下面回放本轮真实的 HTTP 指令、模型规划、MQTT 回执与软件状态，并说明注册实验和部署验证。所有画面均由原始数据生成，顶栏持续标识非连续现场录屏。设备是本机软件实体，当前没有物理执行或六 G 空口实测。')

    simple, multi = r[:2]
    sb, mb = simple['response']['body'], multi['response']['body']
    def runtime_cards(row):
        body = row['response']['body']
        receipt = body.get('receipt', {})
        left = [f"输入：{row['command']}", f"实际后端：{body.get('backend')}",
                (f"HTTP 墙钟 {number(row['response'].get('wall_ms'))} ms", 'metric')]
        if body.get('configured_model'):
            left.append((f"模型：{body['configured_model']}", 'code'))
        left += [(action_line(a), 'code') for a in actual_actions(row)]
        right = [f"回执：{body.get('execution_state', '未提交')}",
                 f"command_id：{body.get('command_id', '无')}",
                 f"完成索引：{receipt.get('completed_indices', [])}",
                 f"软件版本：{receipt.get('state_version_before', '无')} → {receipt.get('state_version_after', '无')}",
                 f"客厅灯：{light_state(row, 'before', '客厅')} → {light_state(row, 'after', '客厅')}",
                 f"卧室灯：{light_state(row, 'before', '卧室')} → {light_state(row, 'after', '卧室')}",
                 ('物理完成：false', 'muted')]
        return [('HTTP 原始请求与执行', left), ('请求绑定回执与当前软件状态', right)]
    p1 = image(2, 'rule', '一条简单指令，经过正式执行通道', '实际计时为 HTTP 请求墙钟；不使用镜头停留时间代替时延。', runtime_cards(simple), 'final_runtime_records.json / records[0]')
    p2 = image(2, 'model', '模型先给计划，再经白名单与回执确认', '这两条动作来自本轮真实模型返回；状态来自独立软件实体。', runtime_cards(multi), 'final_runtime_records.json / records[1]')
    scene([(p1, 18), (p2, 26)], f"先看当前运行。指令是：{simple['command']}。实际走 {sb.get('backend')}，HTTP 墙钟为 {number(simple['response']['wall_ms'])} 毫秒。随后输入：{multi['command']}。真实模型返回 {len(actual_actions(multi))} 条动作，原网关白名单校验后发送给 MQTT 软件实体。回执状态为 {mb.get('execution_state')}，完成索引和软件版本均保存在原始记录里。第二条请求墙钟为 {number(multi['response']['wall_ms'])} 毫秒。页面状态与命令回执分别查询，历史回执不会重新应用旧动作。")

    cards = []
    for row in r[2:]:
        body = row['response']['body']
        answer = body.get('answer') or body.get('refusal_reason') or body.get('error') or '原记录返回空动作结构；不补写澄清文本'
        cards.append(('原始输入与返回', [row['command'], f"动作数：{len(actual_actions(row))}",
                     f"semantic_complete：{str(body.get('semantic_complete')).lower()}",
                     f"HTTP 墙钟：{number(row['response']['wall_ms'])} ms", (str(answer), 'muted'),
                     ('无执行动作；不把 HTTP 200 当任务完成', 'muted')]))
    p = image(3, 'empty', '缺少目标与注入请求，保留原始结果', '空动作结构、安全拒绝与真正完成分别报告。', cards, 'final_runtime_records.json / records[2:4]')
    scene([(p, 28)], '第三条输入缺少房间，第四条是试图绕过规则的注入请求。画面逐字段保留当前模型真实返回，包括动作数量、完成标记和回答内容。没有出现的澄清话术不会由视频补造。空动作以及明确拒绝均保持无动作执行，接口返回二百也不能算任务完成。四条指令都是自动化验证，不计为真人反馈。')

    arm_lines = []
    for arm in ('local-first', 'second-first', 'joint'):
        info = results['by_arm'][arm]
        arm_lines += [(f"{arm}：{info['completed']}/{info['n']}", 'body'),
                      (f"总墙钟中位 {number(info['median_total_wall_ms_all_requests'])} ms", 'muted')]
    p = image(4, 'comparison', '同一恢复器下，只改变初选策略', '三臂共用模型、超时、初态、提示与评分；全部失败保留。', [
        ('先注册，再运行', [(f"{results['n_rows']} 行完整记录", 'metric'),
                    f"主任务 {results['primary_rows']} 行 + 空计划辅助 24 行", '4 条已冻结复杂命令 × 6 条件 × 3 臂',
                    f"运行器失败：{results['runner_failed']}", ('旧人工标签冻结；不是新任务泛化测试', 'muted')]),
        ('主任务完成与总墙钟', arm_lines + [(f"本组暂选：{results['recommendation']['arm']}", 'muted')]),
    ], 'docs/experiments_v4/registration.json；results.json；rows.jsonl')
    scene([(p, 32)], f"对照先于运行注册，共 {results['n_rows']} 行，其中七十二行主任务、二十四行空计划辅助验证。三种策略共享同一恢复器，只改变初选。三臂主任务均完成二十条、共二十四条，失败样本完整保留。包含失败的总墙钟中位数分别约为九百五十六、一千二百六十五和九百七十六毫秒。这一组同机回归暂选本地优先，结论范围仅限注册的命令和受控条件。")

    selected = [next(row for row in bundle['rows'] if row['condition'] == condition and row['arm'] == 'local-first')
                for condition in ('local_post503', 'both_post503')]
    cards = []
    for heading, row in zip(('单端 POST 503 后恢复', '双端 POST 503 后停止'), selected):
        post = [p for p in row['proxy_records'] if p.get('method') == 'POST']
        statuses = ' → '.join(str(p.get('http_status')) for p in post)
        cards.append((heading, [f"原始行：{row['case_id']} / {row['condition']}", f"代理 POST 状态：{statuses}",
                     f"任务完成：{str(row['task_completed']).lower()}", f"执行器调用：{row['executor_calls']}",
                     (f"总墙钟 {number(row['timing_ms']['total_wall'])} ms", 'metric'),
                     ('原始 HTTP 代理故障注入，非无线丢包', 'muted')]))
    p = image(5, 'recovery', '失败行也展示：恢复成功与双端失败', '直接摘录本轮注册实验原始行；本片不重新调用模型。', cards, 'docs/experiments_v4/rows.jsonl / local-first:C01')
    scene([(p, 34)], '恢复段直接摘录本轮注册实验的原始行。左侧是本地模型请求被代理注入五零三之后，备用模型返回二百，最终只调用一次执行器。右侧是两端请求都被注入五零三，系统终止并保留零次执行。两侧总墙钟来自原始分段计时。故障发生在软件 HTTP 代理，不解释成无线丢包；两个模型共享本机资源，也不冒充独立云主机。')

    integration = bundle['integration_dir']
    normal = {row['case']: row['body'] for row in read(integration / 'normal/http_results.json')}
    ack = {row['case']: row['body'] for row in read(integration / 'ack_loss/http_results.json')}
    p = image(6, 'receipt', '持久回执：未知只查，完成不重发', '命令 ID + 原始指令 + 平台身份 + 动作 SHA256 绑定', [
        ('六类故障合同通过', ['重复请求 / 异载荷冲突', '提交后丢 ACK / 网关与实体重启', '部分完成 / 矛盾绑定',
                       ('逐 ID 原始 MQTT submit 均为 1 次', 'metric'), ('SQLite 先记 unknown；软件状态与实体回执原子提交', 'muted')]),
        ('原始记录摘录', [f"丢 ACK：{ack['ack_loss']['execution_state']} → {ack['query_after_ack_loss']['execution_state']}",
                    f"重启后：{normal['after_restart']['execution_state']}，原回执版本仍为 1", '部分执行仅完成索引 [0]，不补做剩余动作',
                    '矛盾 platform_id：保持 unknown', '旧开灯回执之后，当前状态仍为关灯', ('当前状态单独查询，不重放历史动作', 'muted')]),
    ], 'docs/integration_v4/LATEST.json → 原始 HTTP / MQTT / 双 SQLite / offline_audit.json')
    scene([(p, 34)], '持久回执验证重复、冲突、丢确认、重启、部分执行和绑定矛盾。网关在发送之前把未知状态写入 SQLite。确认丢失后只查询，不自动重发。实际重启网关适配器和独立软件实体后，原命令回执仍可读取。原始 MQTT 日志逐个命令核对，提交消息均只有一次。部分执行与矛盾回执不会补做动作。界面查询的是当前软件快照，旧回执不会把较新的关灯状态覆盖。')

    p = image(7, 'operations', '新目录部署与 120 秒规则负载', '自动化部署与软件规则路径验收；不计为真人安装工时或模型长稳测试。', [
        ('本轮自动化验收', [(f"{ops['observed_duration_s']:.3f} s / {ops['dispatched_requests']} 请求", 'metric'),
               f"预期行为：{ops['expected_behavior_observed']}/{ops['dispatched_requests']}",
               f"传输错误：{ops['transport_errors']}", f"进程树 RSS 最高：{ops['max_process_tree_rss_sum_bytes']/1048576:.2f} MiB",
               f"事件增长：{ops['event_growth_bytes']/1024:.2f} KiB", ('规则路径；模型不在本负载范围', 'muted')]),
        ('仍待真实采集', [f"外部真人反馈：{ops['participant_feedback_count']}", '真实总成本：null',
                   '采购、电量、电价证据：待采', '真人独立部署工时：待采', '实物设备 / WAN / 6G：待验收',
                   ('这些缺项保留未知，不用自动化结果代替', 'muted')]),
    ], 'docs/operations_v4/results.json；rows.jsonl；resource_samples.json')
    scene([(p, 32)], '自动化把源码复制到新目录，再启动网关、真实 MQTT 服务与软件实体。固定规则负载持续一百二十秒，发出一百二十个请求，预期行为全部符合，传输错误为零。进程树内存和事件增长保留原始采样。这只是两分钟规则路径验收，不代表模型长期稳定。真人独立部署、外部用户反馈、设备采购和整机能耗仍待采集，实际总成本保持未知。')

    p = image(8, 'delivery', '从结论回到文件，从文件追到原始证据', '方案、答辩、佐证与当前视频统一到 v4；源数据与编辑边界随包保存。', [
        ('交付入口', [('dist/final/20261111_v4/', 'code'), '正式方案 PDF / 答辩 PDF / 佐证 PDF', '当前软件证据回放 MP4',
                 ('docs/video/v4/manifest.json', 'code'), '输入 SHA256、版本、镜头时长、合成语音记录']),
        ('复核入口', [('docs/experiments_v4/', 'code'), ('docs/integration_v4/', 'code'), ('docs/operations_v4/', 'code'),
                 '当前 HTTP 原始记录与 final_capture 源码快照', ('本片只重排与摘录证据，不改写原始数值', 'muted')]),
    ], 'dist/final/20261111_v4/；docs/video/v4/manifest.json')
    scene([(p, 22)], '交付目录中统一保存方案、答辩、佐证与本片。视频清单列出来源文件、版本哈希、每段编辑时长和合成语音信息。评审可从每个结论返回原始请求、MQTT 消息、数据库与注册结果。所有未知项继续保留未知，作品以可运行、可查询、可复核的软件链路接受验收。')

    assert len(scenes) == 8 and [sum(sec for _, sec in items) for items in scenes] == DURATIONS
    return scenes, narration, frames


def speech_files(work, narration, log):
    target = work / 'narration.json'
    write(target, narration)
    ps = r'''$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$paras=Get-Content -LiteralPath '__NARR__' -Raw -Encoding UTF8 | ConvertFrom-Json
$s=New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('Microsoft Huihui Desktop')
$s.Rate=0
$s.Volume=100
for($i=0;$i -lt $paras.Count;$i++) {
 $dest=Join-Path '__WORK__' ('speech_'+($i+1).ToString('00')+'.wav')
 $s.SetOutputToWaveFile($dest)
 $s.Speak($paras[$i])
 $s.SetOutputToNull()
}
$s.Dispose()
'''.replace('__NARR__', str(target).replace("'", "''")).replace('__WORK__', str(work).replace("'", "''"))
    script = work / 'synthetic_voice.ps1'
    script.write_text(ps, encoding='utf-8-sig')
    run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script], log=log)


def tempo_filter(value):
    pieces = []
    while value > 2:
        pieces.append('atempo=2')
        value /= 2
    while value < .5:
        pieces.append('atempo=0.5')
        value /= .5
    return ','.join(pieces + [f'atempo={value:.8f}', 'apad'])


def concat_file(path, clips):
    path.write_text('\n'.join("file '" + p.as_posix().replace("'", "'\\''") + "'" for p in clips), encoding='utf-8')


def build(args):
    bundle = load_bundle(args.records.resolve(), args.runtime_manifest.resolve())
    source_hashes = {rel(path): digest(path) for path in bundle['sources']}
    work = HERE / 'builds' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    work.mkdir(parents=True, exist_ok=False)
    scenes, narration, frames = prepare_scenes(bundle, work)
    write(work / 'scene_records.json', {'frames': frames, 'durations': DURATIONS})
    write(work / 'narration.json', narration)
    if args.prepare_only:
        print(json.dumps({'prepared': rel(work), 'frame_count': len(frames)}, ensure_ascii=False))
        return
    output = OUT / VIDEO_NAME
    OUT.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError('KEEP_EXISTING_FINAL_VIDEO: move it explicitly before rebuilding')
    ff, fp = binaries()
    log = work / 'build.log'
    speech_files(work, narration, log)
    segments, speech_meta = [], []
    for i, (images, duration) in enumerate(zip(scenes, DURATIONS), 1):
        speech = work / f'speech_{i:02d}.wav'
        with wave.open(str(speech), 'rb') as audio:
            raw_seconds = audio.getnframes() / audio.getframerate()
        ratio = raw_seconds / (duration - .4)
        aligned = work / f'aligned_{i:02d}.wav'
        run([ff, '-hide_banner', '-loglevel', 'error', '-y', '-i', speech, '-af', tempo_filter(ratio), '-t', duration, aligned], log=log)
        speech_meta.append({'scene': i, 'raw_seconds': raw_seconds, 'aligned_seconds': duration, 'atempo_ratio': ratio})
        clips = []
        for j, (path, seconds) in enumerate(images):
            clip = work / f'clip_{i:02d}_{j}.mp4'
            run([ff, '-hide_banner', '-loglevel', 'error', '-y', '-loop', '1', '-framerate', '10', '-i', path,
                 '-t', seconds, '-c:v', 'libx264', '-preset', 'fast', '-tune', 'stillimage', '-crf', '21',
                 '-pix_fmt', 'yuv420p', '-r', '10', '-an', clip], log=log)
            clips.append(clip)
        listing = work / f'visual_{i:02d}.txt'
        concat_file(listing, clips)
        segment = work / f'segment_{i:02d}.mp4'
        run([ff, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', listing,
             '-i', aligned, '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k', '-t', duration, segment], log=log)
        segments.append(segment)
        print(f'scene {i}/8 built', flush=True)
    listing = work / 'all_segments.txt'
    concat_file(listing, segments)
    run([ff, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', listing,
         '-c', 'copy', '-movflags', '+faststart', output], log=log)
    probe = json.loads(run([fp, '-v', 'error', '-show_entries', 'format=duration,size', '-show_entries',
                           'stream=width,height,r_frame_rate,codec_name,sample_rate', '-of', 'json', output], log=log))
    seconds = float(probe['format']['duration'])
    assert 180 <= seconds <= 300 and output.stat().st_size <= 300_000_000
    write(work / 'ffprobe.json', probe)
    # Actual encoded-video frames, not just the source PNGs, are saved to inspect.
    extracts, start = [], 0
    for i, duration in enumerate(DURATIONS, 1):
        positions = [start + duration / 2]
        if i == 2:
            positions = [start + 9, start + 31]
        for j, position in enumerate(positions):
            target = work / f'check_{i:02d}_{j}.png'
            run([ff, '-hide_banner', '-loglevel', 'error', '-y', '-ss', position, '-i', output, '-frames:v', '1', target], log=log)
            extracts.append({'scene': i, 'at_seconds': position, 'frame': rel(target)})
        start += duration
    assert source_hashes == {rel(path): digest(path) for path in bundle['sources']}, 'SOURCE_CHANGED_DURING_BUILD'
    manifest = {'schema': 'sensehome.video-manifest.v4', 'video_path': rel(output), 'duration_seconds': seconds,
                'size_bytes': output.stat().st_size, 'sha256': digest(output), 'created_at_utc': datetime.now(timezone.utc).isoformat(),
                'recording_kind': 'current_real_software_records_replay_with_evidence_explanation',
                'version': '20261111_v4', 'source': rel(args.records), 'runtime_manifest': rel(args.runtime_manifest),
                'source_sha256': source_hashes, 'builder_sha256': digest(Path(__file__)), 'source_hashes_unchanged_during_build': True,
                'scene_durations': DURATIONS, 'total_editorial_seconds': sum(DURATIONS), 'frames': frames,
                'editing_boundaries': ['all visual cards drawn from current source fields; no screenshot reconstruction',
                    'scene ordering and dwell times are editorial; displayed runtime milliseconds are unmodified recorded values',
                    'recovery scenes replay registered raw rows without new model inference',
                    'no historical v1 video, desktop screenshot or CUA capture used',
                    'no physical actuator, WAN, independent cloud or 6G air-interface claim'],
                'persistent_header': HEADER, 'synthetic_voice': {'engine': 'Windows System.Speech', 'voice': 'Microsoft Huihui Desktop',
                    'human_voice_recording': False, 'speech_alignment': speech_meta, 'narration': rel(work / 'narration.json')},
                'probe': probe, 'build_directory': rel(work), 'encoded_frame_checks': extracts,
                'visual_inspection': {'status': 'pending agent visual inspection of encoded frames'}}
    write(HERE / 'manifest.json', manifest)
    print(json.dumps({'video': rel(output), 'duration_seconds': seconds, 'size_bytes': output.stat().st_size,
                      'manifest': rel(HERE / 'manifest.json'), 'build_directory': rel(work)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path, default=HERE / 'final_runtime_records.json')
    parser.add_argument('--runtime-manifest', type=Path, default=HERE / 'final_runtime_manifest.json')
    parser.add_argument('--prepare-only', action='store_true')
    build(parser.parse_args())
