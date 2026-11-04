from pathlib import Path
import json, subprocess, wave
ROOT=Path(__file__).resolve().parents[2]
FF=Path('C:/Program Files/EVCapture/ffmpeg.exe')
FP=Path('C:/Program Files/EVCapture/ffprobe.exe')
durations=[25,35,30,55,45,35,20]
def run(args):
 p=subprocess.run([str(FF),'-hide_banner','-loglevel','error','-y',*map(str,args)],cwd=ROOT,capture_output=True,text=True)
 if p.returncode: raise RuntimeError(p.stderr)
for i,target in enumerate(durations,1):
 src=ROOT/'docs/video'/f'narration_{i:02d}.wav'
 with wave.open(str(src),'rb') as w: dur=w.getnframes()/w.getframerate()
 tempo=max(.5,min(2,dur/(target-1)))
 run(['-i',src,'-af',f'atempo={tempo:.6f},apad','-t',target,ROOT/'docs/video'/f'aligned_{i:02d}.wav'])
(ROOT/'docs/video/audio_concat.txt').write_text('\n'.join(f"file 'aligned_{i:02d}.wav'" for i in range(1,8)),encoding='utf-8')
run(['-f','concat','-safe','0','-i','docs/video/audio_concat.txt','-c:a','pcm_s16le','docs/video/narration_aligned.wav'])
run(['-i','_work/video_noaudio.mp4','-i','docs/video/narration_aligned.wav','-c:v','copy','-c:a','aac','-b:a','128k','-t','245','-movflags','+faststart','dist/SenseHome_AI6G_演示视频_20261103.mp4'])
probe=subprocess.check_output([str(FP),'-v','error','-show_entries','format=duration,size','-show_entries','stream=width,height,r_frame_rate,codec_name','-of','json','dist/SenseHome_AI6G_演示视频_20261103.mp4'],cwd=ROOT,text=True)
(ROOT/'docs/video/ffprobe.json').write_text(probe,encoding='utf-8')
print(probe)
