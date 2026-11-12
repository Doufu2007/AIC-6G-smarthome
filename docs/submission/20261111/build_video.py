"""Build a 3-5 minute evidence explanation, using original software replay only."""
from __future__ import annotations
import hashlib,json,subprocess,wave
from pathlib import Path
import fitz
from PIL import Image, ImageDraw, ImageFont
from materials import ROOT

HERE=Path(__file__).resolve().parent
OUT=ROOT/'dist/final/20261111';WORK=ROOT/'_work/submission_20261111/video';WORK.mkdir(parents=True,exist_ok=True)
FF='C:/Program Files/EVCapture/ffmpeg.exe';FP='C:/Program Files/EVCapture/ffprobe.exe'
NARRATIONS=[
'本作品是SenseHome，面向存量住宅的家庭AI推理路由场景设计。系统按任务类别、两段链路和两端算力选择设备规则、网关模型或远程模型，并在动作校验后留下事件。当前证据包括融合策略、独立真实模型与模拟设备。下面按证据层展示。',
'需求对象是家居集成服务商及住户。广域网异常时，局域网和本地控制可能仍然可用；模型在线时，也可能因排队或输出不合规而失败。我们把这些边界分别记录。客户画像属于设计定位，目前没有真实访谈、具名客户或家庭试点。',
'架构分为设备规则、统一网关和两个模型候选。PC网关适配健康观测并调用唯一的C策略，再执行后端请求、白名单检查和事件持久化。设备用隔离的JSON状态模拟。设备MCU与运行模型的主机分开核算，不能用一个小的C对象替代整个网关内存。',
'支持的简单控制和只读查询优先使用规则路径。复杂指令先检查候选健康、观测年龄、队列、内存与并发，再比较已知链路和推理成本。未知数据保持未知。Home Assistant已有本地意图优先和模型回退，本作品的重点是两段链路、双端资源及可审计的场景组合。',
'四类证据回答不同问题。合成回放检验指定轨迹下的策略合同，HTTP桩检验网关墙钟与事件完整性，真实模型检验参考动作一致，PC状态检验模拟执行。接口响应、合同完成、动作一致和物理执行采用不同定义，不能合并成一个成功率。',
'融合版r3包含五万八千八百条合成回放。每臂五千零四十条复杂任务中，联合策略达标百分之八十四点四，仅网络百分之六十二点九八，仅算力百分之六十一点九，无迟滞百分之八十四点五二。迟滞没有提高完成率，联合仍有七百八十六条未解析。这里使用固定规划桩和建模时延，不是语言理解准确率。',
'独立HTTP实验共有九千零八十四个请求。矩阵复杂任务中联合与仅网络分别达标百分之八十点九五和六十一点九。压力组分别为五十二点九八和五十二点五四，收益很小。六百七十九条合同未完成，外层HTTP却都返回二百，因此接口响应不能代表完成。下游五零三是注入故障，不是实际无线丢包。',
'历史真实模型实验使用Qwen二点五、一点五B，在AutoDL四零八零SUPER上运行二十条命令。HTTP成功十七条，参考动作一致十七条，请求中位数二百九十点六七九毫秒。三条四二二属于保护拒绝，预期拒答另列。标签冻结后重新核对原始响应，未伪称再次推理。这组历史实验尚未与r3联测。',
'下面回放四步历史软件记录，采集于十一月三日，网络断开条件为软件注入。第一步开客厅灯，隔离JSON状态由关闭变成开启。第二步只读查询安防，不修改状态。第三步复合指令因缺少规划能力而拒绝局部执行，状态保持不变。第四步关闭客厅灯。画面中的输入、原因和前后状态来自原始JSON。镜头节奏由编辑安排，不代表执行耗时。另有二十八项白名单用例通过，其中二十五项拒绝、三项正向控制；它们不构成物理设备安全认证。',
'AI与六G的贴合点是网络和计算资源共同影响推理位置。当前实现了请求级候选选择、任务分级和带来源的观测。ITU的IMT二零三零框架和ETSI的边缘计算方向提供接口演进依据。当前没有六G空口、网络切片、确定性承载或无线资源调度实测，未来接入真实网络后再验收尾时延与完成率。',
'本机已有双模型服务。三十个短时基线样本中，小模型和大模型进程RSS中位约一点一七和二点七二GB，共享显存已用三千七百三十五MiB。这不是最低配置。成本模型分开计算硬件、电量、调用、维护和网络。采购、电价或电表数据缺失时，总成本保留未知，不发布实测节费比例。',
'当前由一名学生完成策略、网关、测试和材料。r3全量构建与测试通过，运行期间源码没有变化，归档三百二十八个运行文件逐路径哈希一致。第三方框架、模型和公开资料注明来源。生成式AI参与代码、文稿和脚本辅助，本片使用合成语音，最终材料由参赛者复核负责。',
'作品交付是可运行、可解释、可回放的家庭推理路由软件。已保存融合实验、真实模型历史响应与模拟状态证据。后续按同一网关联测真实推理，再取得需求、设备、网络和部署成本证据。方案、答辩、佐证、源码与原始记录保持版本一致，以实际数据决定适用范围。'
]
NARRATIONS.insert(8,'本轮正式本机联测使用二十条冻结命令，四个策略臂各重复三次，共二百四十条，运行器失败和跳过均为零。复杂模型每臂二十一次请求，四臂都完成十五次非空模拟执行。固定四B模型另有三个空结构匹配，不能当作非空执行。规则请求另列，空结构不解释成澄清正确。两个模型共享同机GPU，远程槽位只是第二端点代理。正常健康条件下，没有证明联合策略优势。')
NARRATIONS[4]=NARRATIONS[4].replace('四类证据','各层证据')
NARRATIONS[-1]='作品交付是可运行、可解释、可回放的家庭推理路由软件。已保存融合实验、二百四十条本机真实联测与历史模型响应。后续进行独立主机部署、真实网络与用户试用，以及整机成本验收。方案、答辩、佐证、源码与原始记录保持版本一致，以实际数据决定适用范围。'
DURATIONS=[14,16,17,20,16,22,22,22,24,36,21,18,17,18]

def run(args):
    p=subprocess.run(list(map(str,args)),capture_output=True,text=True,encoding='utf-8',errors='replace')
    if p.returncode:raise RuntimeError(p.stderr)
    return p.stdout
def atempo(v):
    a=[]
    while v>2:a.append('atempo=2');v/=2
    while v<.5:a.append('atempo=0.5');v/=.5
    return ','.join(a+[f'atempo={v:.8f}','apad'])

def prepare():
    (HERE/'narration.json').write_text(json.dumps(NARRATIONS,ensure_ascii=False,indent=2),encoding='utf-8')
    ps='''$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$paras=Get-Content -LiteralPath '__NARR__' -Raw -Encoding UTF8 | ConvertFrom-Json
$s=New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('Microsoft Huihui Desktop')
$s.Rate=0
$s.Volume=100
for($i=0;$i -lt $paras.Count;$i++){
 $dest=Join-Path '__WORK__' ('speech_'+($i+1).ToString('00')+'.wav')
 $s.SetOutputToWaveFile($dest)
 $s.Speak($paras[$i])
 $s.SetOutputToNull()
}
$s.Dispose()
'''.replace('__NARR__',str(HERE/'narration.json')).replace('__WORK__',str(WORK))
    (HERE/'voice.ps1').write_text(ps,encoding='utf-8-sig')
    run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',HERE/'voice.ps1'])
    doc=fitz.open(OUT/'SenseHome_场景设计答辩_20261111.pdf')
    for i,p in enumerate(doc):
        pix=p.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False)
        im=Image.frombytes('RGB',[pix.width,pix.height],pix.samples).resize((1920,1080),Image.Resampling.LANCZOS)
        im.save(WORK/f'slide_{i+1:02d}.png')
    for i in range(1,5):
        src=ROOT/f'_work/video_v2_frames/replay_{i:02d}.png'
        if not src.exists():run([sys.executable,ROOT/'docs/video/v2/render_replay.py'])
        im=Image.open(src).convert('RGB');d=ImageDraw.Draw(im);f=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',26)
        d.rectangle((0,0,1920,40),fill='#07111f');d.text((75,4),'历史记录 2026-11-03  软件状态回放  非r3现场录屏',font=f,fill='#ffbf72')
        im.save(WORK/f'replay_{i:02d}.png')

def build():
    prepare();segments=[]
    for i,target in enumerate(DURATIONS,1):
        speech=WORK/f'speech_{i:02d}.wav'
        with wave.open(str(speech),'rb') as w:seconds=w.getnframes()/w.getframerate()
        aligned=WORK/f'aligned_{i:02d}.wav'
        run([FF,'-hide_banner','-loglevel','error','-y','-i',speech,'-af',atempo(seconds/(target-.25)),'-t',target,aligned])
        visuals=[(WORK/f'slide_{i:02d}.png',target)] if i!=10 else [(WORK/f'replay_{n:02d}.png',sec) for n,sec in enumerate([8,9,10,9],1)]
        video_parts=[]
        for j,(im,sec) in enumerate(visuals):
            clip=WORK/f'clip_{i:02d}_{j}.mp4'
            run([FF,'-hide_banner','-loglevel','error','-y','-loop','1','-framerate','10','-i',im,'-t',sec,'-c:v','libx264','-preset','fast','-tune','stillimage','-crf','21','-pix_fmt','yuv420p','-r','10','-an',clip]);video_parts.append(clip)
        concat=WORK/f'visual_{i:02d}.txt';concat.write_text('\n'.join("file '"+p.as_posix()+"'" for p in video_parts),encoding='utf-8')
        segment=WORK/f'segment_{i:02d}.mp4'
        run([FF,'-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',concat,'-i',aligned,'-c:v','copy','-c:a','aac','-b:a','128k','-t',target,segment]);segments.append(segment)
        print('scene',i,'of',len(DURATIONS),flush=True)
    concat=WORK/'all_segments.txt';concat.write_text('\n'.join("file '"+p.as_posix()+"'" for p in segments),encoding='utf-8')
    video=OUT/'SenseHome_演示视频_20261111.mp4'
    run([FF,'-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',concat,'-c','copy','-movflags','+faststart',video])
    probe=json.loads(run([FP,'-v','error','-show_entries','format=duration,size','-show_entries','stream=width,height,r_frame_rate,codec_name','-of','json',video]))
    assert 180<=float(probe['format']['duration'])<=300
    assert video.stat().st_size<=300000000
    manifest={'version':'20261111','duration_s':float(probe['format']['duration']),'size_bytes':video.stat().st_size,'sha256':hashlib.sha256(video.read_bytes()).hexdigest(),'probe':probe,'method':'evidence explanation slides, synthetic voice, 2026-11-03 actual software JSON state replay; no physical actuator or 6G air interface','scene_durations':DURATIONS,'narration':str((HERE/'narration.json').relative_to(ROOT)),'replay_source':'docs/video/v2/software_replay.json','replay_sha256':hashlib.sha256((ROOT/'docs/video/v2/software_replay.json').read_bytes()).hexdigest()}
    (OUT/'video_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=True,indent=2))
if __name__=='__main__':
    import sys
    build()
