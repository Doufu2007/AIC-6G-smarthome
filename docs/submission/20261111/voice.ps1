$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$paras=Get-Content -LiteralPath 'C:\Users\郑傅韬\Desktop\大学生活\AIC\AIC6G专项赛_2026\场景赛道\docs\submission\20261111\narration.json' -Raw -Encoding UTF8 | ConvertFrom-Json
$s=New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('Microsoft Huihui Desktop')
$s.Rate=0
$s.Volume=100
for($i=0;$i -lt $paras.Count;$i++){
 $dest=Join-Path 'C:\Users\郑傅韬\Desktop\大学生活\AIC\AIC6G专项赛_2026\场景赛道\_work\submission_20261111\video' ('speech_'+($i+1).ToString('00')+'.wav')
 $s.SetOutputToWaveFile($dest)
 $s.Speak($paras[$i])
 $s.SetOutputToNull()
}
$s.Dispose()
