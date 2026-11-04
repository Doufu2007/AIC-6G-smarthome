from __future__ import annotations
import hashlib, json, textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'_work/video_v2_frames'; OUT.mkdir(parents=True,exist_ok=True)
DATA=json.loads((ROOT/'docs/video/v2/software_replay.json').read_text(encoding='utf-8'))
F='C:/Windows/Fonts/msyh.ttc'; FB='C:/Windows/Fonts/msyhbd.ttc'
def font(n,b=False): return ImageFont.truetype(FB if b else F,n)
def ct(s,n=45):
    # deterministic character wrapping for Chinese and JSON content
    lines=[]
    for p in str(s).splitlines():
        while p:
            lines.append(p[:n]); p=p[n:]
    return '\n'.join(lines)
def lamp(s,room):
    x=s['lights'][room]
    return ('开启' if x['is_on'] else '关闭')+' / '+str(x['brightness'])+'%'
for row in DATA['rows']:
    i=row['step']; r=row['result']; before=row['state_before']; after=row['state_after']
    im=Image.new('RGB',(1920,1080),(7,17,31)); d=ImageDraw.Draw(im)
    d.rectangle((0,0,1920,12),fill=(30,210,190))
    d.text((75,45),'SenseHome  |  真实软件事件回放',font=font(35,True),fill=(123,224,211))
    d.text((75,105),f'步骤 {i}/4  ·  {row["operation"]}',font=font(48,True),fill=(242,247,250))
    d.rounded_rectangle((1090,48,1840,115),18,fill=(49,43,52),outline=(255,176,74),width=2)
    d.text((1120,67),'PC模拟设备 / 非现场录屏',font=font(32,True),fill=(255,194,107))
    d.text((75,180),'当前 Dashboard process_command → C policy_cli → 规则 → 隔离 JSON 状态',font=font(27),fill=(174,196,211))
    # left actual command/result
    d.rounded_rectangle((75,250,1175,885),20,fill=(14,31,46),outline=(63,103,124),width=2)
    d.text((105,280),'INPUT / 实际命令',font=font(27,True),fill=(30,210,190))
    d.text((105,329),row['command'],font=font(33,True),fill=(241,246,250))
    d.text((105,400),'OUTPUT / 实际返回摘录',font=font(27,True),fill=(30,210,190))
    # exact fields from result, formatting only
    excerpt={'backend':r['backend'],'reason':r['reason'],'interface_ok':r['interface_ok'],'semantic_complete':r['semantic_complete'],'actions':r['actions']}
    lines=json.dumps(excerpt,ensure_ascii=False,indent=2).splitlines()
    y=450
    for line in lines:
        for sub in ct(line,51).splitlines():
            d.text((110,y),sub,font=font(25),fill=(220,236,242)); y+=34
    # right state card
    d.rounded_rectangle((1230,250,1840,885),20,fill=(24,57,72),outline=(30,210,190),width=2)
    d.text((1260,280),'模拟状态：前 → 后',font=font(30,True),fill=(255,194,107))
    yy=350
    for room in ['客厅','卧室']:
        d.text((1260,yy),room+'灯',font=font(28,True),fill=(240,246,250)); yy+=45
        d.text((1260,yy),lamp(before,room)+'  →  '+lamp(after,room),font=font(26),fill=(162,231,218)); yy+=75
    if i==2:
        d.text((1260,605),'安防：未布防 → 未布防',font=font(27),fill=(162,231,218))
        d.text((1260,655),'只读查询，状态未改变',font=font(27,True),fill=(255,194,107))
    elif i==3:
        d.text((1260,605),'state_before == state_after',font=font(26),fill=(162,231,218))
        d.text((1260,655),'拒绝局部执行，状态未改变',font=font(27,True),fill=(255,194,107))
    else:
        d.text((1260,605),'状态由真实规则路径写入',font=font(27),fill=(162,231,218))
    d.text((1260,740),'本次 process_command 墙钟',font=font(24),fill=(173,196,209))
    d.text((1260,785),f'{row["wall_ms"]:.3f} ms  (n=1)',font=font(32,True),fill=(255,194,107))
    d.text((75,915),'事件ID：'+r['event_id']+'  |  WAN=false / LAN=false（注入）',font=font(26),fill=(173,196,209))
    d.text((75,963),'回放节奏为编辑安排，非实际执行耗时；完整结果/状态/事件保留为 JSON',font=font(25),fill=(255,194,107))
    d.text((75,1015),'数据源：docs/video/v2/software_replay.json；无真实模型调用，无物理执行器',font=font(23),fill=(160,181,196))
    im.save(OUT/f'replay_{i:02d}.png',optimize=True)
print('4 replay frames rendered')
