$ErrorActionPreference = 'Stop'
$src = 'C:\Users\b1596\Desktop\【9】知识赛网考题库参考\市调大赛知识赛网考题库及答案3（262页）.doc'
$out = 'C:\Users\b1596\Desktop\视频内容总结\_tiku\市调大赛知识赛网考题库及答案3（262页）.txt'

$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
try {
    $doc = $word.Documents.Open($src, $false, $true)
    # 7 = wdFormatUnicodeText
    $doc.SaveAs2($out, 7)
    $doc.Close($false)
    Write-Output "OK $out"
} finally {
    $word.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
}
