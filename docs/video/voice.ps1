$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$paras=((Get-Content docs/video/narration.txt -Raw -Encoding UTF8) -split "`r?`n`r?`n") | Where-Object {$_.Trim()}
$s=New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('Microsoft Huihui Desktop')
$s.Rate=-1
$s.Volume=100
for($i=0;$i -lt $paras.Count;$i++){
 $dest=Join-Path (Get-Location) ('docs/video/narration_'+($i+1).ToString('00')+'.wav')
 $s.SetOutputToWaveFile($dest)
 $s.Speak($paras[$i])
 $s.SetOutputToNull()
}
$s.Dispose()
Write-Output ('Generated '+$paras.Count+' speech segments')
