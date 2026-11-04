from __future__ import annotations
import hashlib, json, subprocess, wave
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
FF='C:/Program Files/EVCapture/ffmpeg.exe'; FP='C:/Program Files/EVCapture/ffprobe.exe'
V1=ROOT/'dist/SenseHome_AI6G_演示视频_20261103.mp4'
V2=ROOT/'dist/SenseHome_AI6G_演示视频_20261103_v2.mp4'
OUT=ROOT/'docs/video/v2'
durations=[8,8,11,8]
def run(args):
    p=subprocess.run([FF,'-hide_banner','-loglevel','error','-y',*map(str,args)],cwd=ROOT,capture_output=True,text=True)
    if p.returncode: raise RuntimeError(p.stderr)
for i,target in enumerate(durations,1):
    src=OUT/f'speech_{i:02d}.wav'
    with wave.open(str(src),'rb') as w: duration=w.getnframes()/w.getframerate()
    tempo=duration/(target-.5)
    if not .5 <= tempo <=2: raise RuntimeError(f'Unsupported tempo {tempo}')
    run(['-i',src,'-af',f'atempo={tempo:.7f},apad','-t',target,OUT/f'aligned_{i:02d}.wav'])
(OUT/'audio_scene2.txt').write_text('\n'.join(f"file 'aligned_{i:02d}.wav'" for i in range(1,5)),encoding='utf-8')
run(['-f','concat','-safe','0','-i','docs/video/v2/audio_scene2.txt','-c:a','pcm_s16le','docs/video/v2/scene2.wav'])
# Preserve v1 audio outside the edited 35-second scene.
run(['-i',V1,'-i',OUT/'scene2.wav','-filter_complex','[0:a]asplit=2[aa][bb];[aa]atrim=0:25,asetpts=PTS-STARTPTS[a];[bb]atrim=60:245,asetpts=PTS-STARTPTS[b];[a][1:a][b]concat=n=3:v=0:a=1[out]','-map','[out]','-c:a','pcm_s16le',OUT/'full_narration_v2.wav'])
# Only generated frames and known video input are used; no desktop/browser UI.
args=['-i',V1]
for i,t in enumerate(durations,1): args+=['-loop','1','-t',str(t),'-i',f'_work/video_v2_frames/replay_{i:02d}.png']
args+=['-i',OUT/'full_narration_v2.wav','-filter_complex','[0:v]split=2[aa][bb];[aa]trim=0:25,setpts=PTS-STARTPTS[a];[bb]trim=60:245,setpts=PTS-STARTPTS[b];[1:v]setpts=PTS-STARTPTS[p1];[2:v]setpts=PTS-STARTPTS[p2];[3:v]setpts=PTS-STARTPTS[p3];[4:v]setpts=PTS-STARTPTS[p4];[a][p1][p2][p3][p4][b]concat=n=6:v=1:a=0[out]','-map','[out]','-map','5:a','-r','30','-c:v','libx264','-preset','medium','-crf','22','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-t','245','-movflags','+faststart',V2]
run(args)
probe=json.loads(subprocess.check_output([FP,'-v','error','-show_entries','format=duration,size','-show_entries','stream=width,height,r_frame_rate,codec_name','-of','json',str(V2)],cwd=ROOT,text=True))
assert 180 <= float(probe['format']['duration']) <=300
assert int(probe['format']['size']) <=300000000
manifest={'version':2,'generated_at':'2026-11-03','method':'7 scene evidence explanation; scene 2 replaced with sequential actual software JSON/state replay; not live recording','scope':'PC simulated device; no physical actuator; no real model; injected WAN/LAN all-down','duration_s':float(probe['format']['duration']),'size_bytes':V2.stat().st_size,'sha256':hashlib.sha256(V2.read_bytes()).hexdigest(),'preserved_v1_sha256':hashlib.sha256(V1.read_bytes()).hexdigest(),'replay_interval_s':[25,60],'replay_step_durations_s':durations,'evidence_sha256':hashlib.sha256((OUT/'software_replay.json').read_bytes()).hexdigest(),'events_sha256':hashlib.sha256((OUT/'events.jsonl').read_bytes()).hexdigest(),'probe':probe}
(OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(manifest,ensure_ascii=True,indent=2))
