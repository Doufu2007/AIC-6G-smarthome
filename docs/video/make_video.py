from __future__ import annotations
import json, os, textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'_work'/'video_frames'
OUT.mkdir(parents=True,exist_ok=True)
W,H=1920,1080
FONT='C:/Windows/Fonts/msyh.ttc'
FONT_B='C:/Windows/Fonts/msyhbd.ttc'

def f(size,bold=False): return ImageFont.truetype(FONT_B if bold else FONT,size)

def wrap(s,width=28):
    return '\n'.join(textwrap.wrap(s,width=width,break_long_words=False,break_on_hyphens=False))

def base(idx,title,kicker,tag):
    im=Image.new('RGB',(W,H),(7,17,31)); d=ImageDraw.Draw(im)
    # top accent and grid
    d.rectangle((0,0,W,12),fill=(30,210,190))
    for x in range(0,W,120): d.line((x,120,x,H-80),fill=(12,37,57),width=1)
    for y in range(120,H-80,80): d.line((0,y,W,y),fill=(12,37,57),width=1)
    d.text((86,55),f'AI + 6G  /  SenseHome',font=f(30,True),fill=(112,221,208))
    d.text((86,106),f'{idx:02d}',font=f(34,True),fill=(255,176,74))
    d.text((155,103),title,font=f(54,True),fill=(240,246,250))
    d.text((86,178),kicker,font=f(27),fill=(169,190,207))
    d.rounded_rectangle((1450,55,1835,112),18,fill=(22,45,64),outline=(71,111,132),width=2)
    d.text((1480,69),tag,font=f(25,True),fill=(255,194,107))
    return im,d

def footer(d,claim='本片仅展示本地台架与模拟设备证据；真实模型/真实6G空口待验证'):
    d.line((86,H-78,W-86,H-78),fill=(43,76,96),width=2)
    d.text((86,H-60),claim,font=f(22),fill=(159,178,191))

def card(d,x,y,w,h,head,body,color=(34,73,95),accent=(30,210,190)):
    d.rounded_rectangle((x,y,x+w,y+h),18,fill=color,outline=(62,105,126),width=2)
    d.rectangle((x,y,x+10,y+h),fill=accent)
    d.text((x+30,y+22),head,font=f(28,True),fill=(255,196,108))
    d.multiline_text((x+30,y+74),wrap(body,22),font=f(28),fill=(232,240,244),spacing=12)

def scene1():
    im,d=base(1,'业务触发：WAN 断开时，入口仍要可用','受控故障注入 / 0:00—0:25','受控基线')
    card(d,120,270,730,470,'静态云端路径','用户命令\n“有点冷”\n\nWAN → 不可达\n请求失败，原因码保留',color=(47,47,67),accent=(242,104,95))
    card(d,1010,270,730,470,'自适应入口','同一命令进入网关\n\n网络状态 → C 策略\n\n本地规则保持入口\n降级原因可解释',color=(26,70,74),accent=(30,210,190))
    d.line((850,505,1005,505),fill=(255,176,74),width=8); d.polygon([(1005,505),(970,487),(970,523)],fill=(255,176,74))
    footer(d,'画面为受控基线：不代表所有智能家居产品都会整体失效')
    return im

def scene2():
    im,d=base(2,'一个入口：路由、检查、执行、记录','原型闭环 / 0:25—1:00','模拟设备')
    xs=[120,560,1000,1440]; labels=['用户指令','C 策略','Python 网关','Dashboard']
    for i,(x,l) in enumerate(zip(xs,labels)):
        d.rounded_rectangle((x,320,x+300,570),22,fill=(24,57,78),outline=(79,127,143),width=3)
        d.text((x+44,365),l,font=f(33,True),fill=(240,246,250))
        body=['结构化/复杂/安全','WAN_OK\nWAN_RTT_HIGH\nLAN_DOWN','调用·校验·回退\n事件ID·时延分段','白名单动作\n状态可见'][i]
        d.multiline_text((x+44,435),body,font=f(26),fill=(157,225,214),spacing=12)
        if i<3:
            d.line((x+300,445,x+430,445),fill=(255,176,74),width=7); d.polygon([(x+430,445),(x+400,428),(x+400,462)],fill=(255,176,74))
    d.rounded_rectangle((200,690,1720,830),20,fill=(35,44,62),outline=(79,102,122),width=2)
    d.text((255,720),'演示状态：灯 / 窗帘 / 温湿度为 PC JSON 模拟设备；动作执行由白名单决定',font=f(30,True),fill=(255,194,107))
    footer(d,'完整网关资源与真实物理执行器尚未测量')
    return im

def scene3():
    im,d=base(3,'为什么有三档：网络状态变成推理位置输入','自适应策略 / 1:00—1:30','设计+实测')
    # central state machine
    d.ellipse((720,260,1200,740),fill=(24,57,78),outline=(30,210,190),width=6)
    d.text((818,370),'任务分级\n+ 网络预算',font=f(46,True),fill=(237,247,249),align='center')
    nodes=[(160,290,'简单 / 安全','local-rules',(30,210,190)),(1380,290,'WAN_OK','cloud-model',(255,176,74)),(1380,650,'WAN 劣化\nLAN 可用','local-model',(130,180,255)),(160,650,'全断 / LAN_DOWN','local-rules\n可解释降级',(242,104,95))]
    for x,y,h,b,c in nodes:
        d.rounded_rectangle((x,y,x+360,y+170),18,fill=(31,48,66),outline=c,width=4)
        d.text((x+28,y+24),h,font=f(28,True),fill=(255,196,108))
        d.text((x+28,y+90),b,font=f(27),fill=(229,240,246))
    for x1,y1,x2,y2 in [(520,375,720,420),(1200,375,1200,430),(1200,735,1200,700),(520,735,720,650)]:
        d.line((x1,y1,x2,y2),fill=(255,176,74),width=7)
    footer(d,'“选择后端”与“任务完成”分列记录；模型自报成功不计分')
    return im

def scene4():
    im,d=base(4,'四档台架：把探测、策略、后端都计入用户时延','n=20 / TCP 链路台架 / 1:30—2:25','桩合同')
    headers=['档位','adaptive 复杂任务','static-cloud 复杂任务','能力说明']
    col=[120,470,850,1250]; widths=[300,330,370,520]
    for x,w,h in zip(col,widths,headers): d.rectangle((x,260,x+w,330),fill=(30,83,95)); d.text((x+20,278),h,font=f(25,True),fill=(245,250,250))
    rows=[('P1 正常','405.6 ms','346.4 ms','探测增加成本'),('P2 劣化','596.2 ms','725.6 ms','自适应转 local-model'),('P3 WAN 断','195.0 ms','失败返回','LAN 仍可用，接口 20/20'),('P4 全断','29.6 ms','失败返回','复杂语义 0/20；规则仍响应')]
    for ri,row in enumerate(rows):
        y=330+ri*120; bg=(19,38,55) if ri%2==0 else (25,48,65)
        for x,w,val in zip(col,widths,row):
            d.rectangle((x,y,x+w,y+120),fill=bg,outline=(50,85,104),width=1)
            d.multiline_text((x+20,y+28),val,font=f(27,True if ri==2 and x==850 else False),fill=(234,242,246),spacing=8)
    d.rounded_rectangle((120,850,1800,935),16,fill=(54,44,52),outline=(242,104,95),width=2)
    d.text((155,875),'所有云/网关耗时均为桩合同；不能据此宣称真实模型准确率',font=f(29,True),fill=(255,194,107))
    footer(d,'数据源：results.json；原始结果 SHA256=df0d74d1…')
    return im

def scene5():
    im,d=base(5,'故障与证据：异常输出先拦截，再回退','安全边界 / 2:25—3:10','可审计')
    card(d,120,260,510,500,'① 故障注入','上游超时\n畸形 JSON\n越权动作\n\n→ error / reason_code\n→ fallback_from',color=(56,44,57),accent=(242,104,95))
    card(d,705,260,510,500,'② 证据记录','事件 ID\n来源与模型 ID\n分段墙钟\n构建哈希\n\nJSON 可回放',color=(33,64,78),accent=(255,176,74))
    card(d,1290,260,510,500,'③ 本地补证','开关灯：PC p95 ≤ 4.434 ms\n只读安防查询：≤ 1.711 ms\n\n模拟 JSON 状态\n不是物理设备',color=(27,70,69),accent=(30,210,190))
    footer(d,'Python 动作检查与原 QEMU C 白名单是两个实现；未宣称覆盖所有注入风险')
    return im

def scene6():
    im,d=base(6,'6G 关联：把“可用预算”变成应用层决策','设计边界 / 3:10—3:45','设计示意')
    # network path
    boxes=[(120,'设备'),(430,'LAN'),(760,'家庭网关'),(1120,'WAN / 5G / 6G'),(1530,'云端')]
    for i,(x,t) in enumerate(boxes):
        d.rounded_rectangle((x,395,x+250,560),18,fill=(27,60,78),outline=(30,210,190) if i!=3 else (255,176,74),width=4)
        d.text((x+55,455),t,font=f(29,True),fill=(240,246,250))
        if i<len(boxes)-1:
            d.line((x+250,478,boxes[i+1][0]-10,478),fill=(255,176,74),width=8); d.polygon([(boxes[i+1][0]-10,478),(boxes[i+1][0]-40,460),(boxes[i+1][0]-40,496)],fill=(255,176,74))
    d.rounded_rectangle((300,680,1600,810),20,fill=(54,44,52),outline=(242,104,95),width=2)
    d.text((350,716),'已实现：应用层路由与回退    |    待验证：真实模型、真实网络、6G 空口',font=f(30,True),fill=(255,194,107))
    footer(d,'当前验证是 TCP 台架；不等同确定性承载、网络切片或真实空天地一体化')
    return im

def scene7():
    im,d=base(7,'下一步：把“可解释降级”做成可测量闭环','交付边界 / 3:45—4:05','下一步')
    items=[('01','真实模型','语义准确率、p50/p95、GPU 与 revision'),('02','同步探测','复杂任务降低探测成本，保留分段口径'),('03','部署补证','网关资源、成本、需求与现场证据')]
    for i,(n,h,b) in enumerate(items):
        x=140+i*575
        d.rounded_rectangle((x,300,x+500,650),22,fill=(26,58,76),outline=(30,210,190),width=3)
        d.text((x+30,340),n,font=f(42,True),fill=(255,176,74)); d.text((x+30,420),h,font=f(34,True),fill=(239,247,250)); d.multiline_text((x+30,500),wrap(b,16),font=f(26),fill=(176,224,218),spacing=12)
    d.rounded_rectangle((340,770,1580,880),20,fill=(30,83,95),outline=(30,210,190),width=2)
    d.text((405,806),'目标：每次降级都能解释、复现、恢复',font=f(36,True),fill=(240,250,250))
    footer(d,'结束卡：作品名 / AI+6G 场景设计 / 证据边界清晰')
    return im

scenes=[scene1,scene2,scene3,scene4,scene5,scene6,scene7]
for i,fn in enumerate(scenes,1):
    fn().save(OUT/f'scene_{i:02d}.png',optimize=True)
manifest={'title':'6G链路自适应的家庭AI智能推理卸载系统','durations_s':[25,35,30,55,45,35,20],'fps':30,'resolution':'1920x1080','generated_at':'2026-11-03','evidence_scope':'local TCP bench + PC JSON simulated device; stub model contract; real model pending','frames':[str((OUT/f'scene_{i:02d}.png').relative_to(ROOT)) for i in range(1,8)]}
(ROOT/'_work'/'video_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(manifest,ensure_ascii=False))
