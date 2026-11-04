$ErrorActionPreference='Stop'
$ffmpeg='C:\Program Files\EVCapture\ffmpeg.exe'
python docs/video/make_video.py
powershell -NoProfile -ExecutionPolicy Bypass -File docs/video/voice.ps1
& $ffmpeg -hide_banner -loglevel error -y -loop 1 -t 25 -i '_work/video_frames/scene_01.png' -loop 1 -t 35 -i '_work/video_frames/scene_02.png' -loop 1 -t 30 -i '_work/video_frames/scene_03.png' -loop 1 -t 55 -i '_work/video_frames/scene_04.png' -loop 1 -t 45 -i '_work/video_frames/scene_05.png' -loop 1 -t 35 -i '_work/video_frames/scene_06.png' -loop 1 -t 20 -i '_work/video_frames/scene_07.png' -filter_complex '[0:v][1:v][2:v][3:v][4:v][5:v][6:v]concat=n=7:v=1:a=0[v]' -map '[v]' -r 30 -c:v libx264 -preset medium -crf 22 -pix_fmt yuv420p '_work/video_noaudio.mp4'
if($LASTEXITCODE){throw 'Video encoding failed'}
python docs/video/align_audio.py
if($LASTEXITCODE){throw 'Audio alignment/mux failed'}
